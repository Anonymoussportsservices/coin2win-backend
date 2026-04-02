import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres@localhost/coin2win"
)

AUTH_SECRET = os.getenv("AUTH_SECRET", "coin2win-dev-secret-change-me")
AUTH_ALGORITHM = "HS256"
AUTH_EXPIRE_HOURS = int(os.getenv("AUTH_EXPIRE_HOURS", "72"))

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

auth_router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: EmailStr
    username: str
    password: str
    referral_code: str | None = None
    registered_host: str | None = None


class LoginBody(BaseModel):
    email: EmailStr
    password: str
    registered_host: str | None = None


def init_auth_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS c2w_users (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(user_id: str, email: str, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "username": username,
        "exp": now + timedelta(hours=AUTH_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, AUTH_SECRET, algorithm=AUTH_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, AUTH_SECRET, algorithms=[AUTH_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")




def resolve_signup_owner(db, referral_code: str | None, registered_host: str | None):
    # 1. Try referral_code
    if referral_code:
        row = db.execute(text("""
            SELECT id
            FROM users
            WHERE agent_code = :code
            LIMIT 1
        """), {"code": referral_code}).mappings().first()

        if row:
            return row["id"], "referral"

    # 2. Try active brand/domain owner
    host = str(registered_host or "").strip().lower()
    if host:
        host = host.split(":")[0].strip()
        brand = db.execute(text("""
            SELECT owner_user_id
            FROM brand_domains
            WHERE lower(domain) = lower(:host)
              AND is_active = true
            LIMIT 1
        """), {"host": host}).mappings().first()

        if brand and brand.get("owner_user_id"):
            return brand["owner_user_id"], "brand_domain"

    # 3. fallback to super_admin
    row = db.execute(text("""
        SELECT id
        FROM users
        WHERE role = 'super_admin'
        ORDER BY created_at ASC NULLS LAST, id ASC
        LIMIT 1
    """)).mappings().first()

    if row:
        return row["id"], "fallback"

    # 4. hard fail
    return None, "none"



def login_host_allows_user(db, user_id: str, registered_host: str | None):
    host = str(registered_host or "").strip().lower()
    if not host:
        return True, "generic"

    host = host.split(":")[0].strip()

    brand = db.execute(text("""
        SELECT id, owner_user_id, domain
        FROM brand_domains
        WHERE lower(domain) = lower(:host)
          AND is_active = true
        LIMIT 1
    """), {"host": host}).mappings().first()

    # Generic / non-branded host => allow
    if not brand:
        return True, "generic"

    # Branded host => only allow users in that owner's subtree
    row = db.execute(text("""
        WITH RECURSIVE user_tree AS (
            SELECT id, parent_id
            FROM users
            WHERE id = :owner_user_id

            UNION ALL

            SELECT u.id, u.parent_id
            FROM users u
            INNER JOIN user_tree ut ON u.parent_id = ut.id
        )
        SELECT id
        FROM user_tree
        WHERE id = :user_id
        LIMIT 1
    """), {
        "owner_user_id": brand["owner_user_id"],
        "user_id": user_id,
    }).fetchone()

    return bool(row), "brand_domain"


def ensure_player_bridge_records(db, user_id: str) -> None:
    owner_row = db.execute(text("""
        SELECT id
        FROM users
        WHERE role = 'super_admin'
        ORDER BY created_at ASC NULLS LAST, id ASC
        LIMIT 1
    """)).mappings().first()

    owner_id = owner_row["id"] if owner_row else None

    existing_user = db.execute(text("""
        SELECT id
        FROM users
        WHERE id = :user_id
        LIMIT 1
    """), {"user_id": user_id}).fetchone()

    if not existing_user:
        db.execute(text("""
            INSERT INTO users (
                id,
                role,
                parent_id,
                created_by,
                is_active,
                billing_type
            )
            VALUES (
                :id,
                'player',
                :parent_id,
                :created_by,
                TRUE,
                'ggr'
            )
        """), {
            "id": user_id,
            "parent_id": owner_id,
            "created_by": owner_id or "system",
        })

    existing_wallet = db.execute(text("""
        SELECT id
        FROM wallets
        WHERE user_id = :user_id
        LIMIT 1
    """), {"user_id": user_id}).fetchone()

    if not existing_wallet:
        db.execute(text("""
            INSERT INTO wallets (
                user_id,
                balance_total,
                balance_pending,
                balance_available
            )
            VALUES (
                :user_id,
                0,
                0,
                0
            )
        """), {"user_id": user_id})

def get_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


@auth_router.post("/register")
def register(body: RegisterBody):
    email = body.email.strip().lower()
    username = body.username.strip()
    password = body.password

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user_id = f"C2W{uuid.uuid4().hex[:10].upper()}"
    referral_code = (body.referral_code or "").strip() or None
    registered_host = (body.registered_host or "").strip().lower() or None
    password_hash = hash_password(password)

    with SessionLocal.begin() as db:
        existing = db.execute(
            text("""
                SELECT id
                FROM c2w_users
                WHERE email = :email OR username = :username
                LIMIT 1
            """),
            {"email": email, "username": username}
        ).fetchone()

        if existing:
            raise HTTPException(status_code=400, detail="Email or username already exists")

        db.execute(
            text("""
                INSERT INTO c2w_users (user_id, email, username, password_hash)
                VALUES (:user_id, :email, :username, :password_hash)
            """),
            {
                "user_id": user_id,
                "email": email,
                "username": username,
                "password_hash": password_hash,
            }
        )

        owner_id, source = resolve_signup_owner(db, referral_code, registered_host)

        ensure_player_bridge_records(db, user_id)

        # update ownership after creation
        if owner_id:
            db.execute(text("""
                UPDATE users
                SET parent_id = :parent_id,
                    created_by = :created_by
                WHERE id = :user_id
            """), {
                "parent_id": owner_id,
                "created_by": owner_id,
                "user_id": user_id,
            })

    token = create_access_token(user_id=user_id, email=email, username=username)

    return {
        "ok": True,
        "token": token,
        "signup_source": source,
        "user": {
            "user_id": user_id,
            "email": email,
            "username": username,
        },
    }


@auth_router.post("/login")
def login(body: LoginBody):
    email = body.email.strip().lower()
    registered_host = (body.registered_host or "").strip().lower() or None

    with SessionLocal() as db:
        row = db.execute(
            text("""
                SELECT user_id, email, username, password_hash, is_active
                FROM c2w_users
                WHERE email = :email
                LIMIT 1
            """),
            {"email": email}
        ).mappings().first()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is inactive")

    if not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    host_allowed, login_scope = login_host_allows_user(db, row["user_id"], registered_host)
    if not host_allowed:
        raise HTTPException(
            status_code=403,
            detail="This account is not assigned to this site. Please log in from the correct platform."
        )

    token = create_access_token(
        user_id=row["user_id"],
        email=row["email"],
        username=row["username"],
    )

    return {
        "ok": True,
        "token": token,
        "login_scope": login_scope,
        "user": {
            "user_id": row["user_id"],
            "email": row["email"],
            "username": row["username"],
        },
    }


@auth_router.get("/me")
def me(authorization: Optional[str] = Header(default=None)):
    token = get_bearer_token(authorization)
    payload = decode_token(token)

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    with SessionLocal() as db:
        row = db.execute(
            text("""
                SELECT user_id, email, username, is_active, created_at
                FROM c2w_users
                WHERE user_id = :user_id
                LIMIT 1
            """),
            {"user_id": user_id}
        ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is inactive")

    return {
        "ok": True,
        "user": {
            "user_id": row["user_id"],
            "email": row["email"],
            "username": row["username"],
            "created_at": str(row["created_at"]),
        },
    }
