import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, text

load_dotenv("/var/www/coin2win/.env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in .env")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

router = APIRouter()

RATE_LIMIT = defaultdict(list)

def check_rate_limit(ip: str, limit=5, window=60):
    now = time.time()
    RATE_LIMIT[ip] = [r for r in RATE_LIMIT[ip] if now - r < window]
    if len(RATE_LIMIT[ip]) >= limit:
        return False
    RATE_LIMIT[ip].append(now)
    return True

class AgentLoginBody(BaseModel):
    email: str
    password: str

@router.post("/agent/auth/login")
def agent_login(body: AgentLoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many requests")

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT
                u.id,
                cu.email,
                cu.username,
                cu.password_hash,
                u.role,
                COALESCE(u.is_active, TRUE) AS user_is_active,
                COALESCE(cu.is_active, TRUE) AS auth_is_active
            FROM users u
            INNER JOIN c2w_users cu
                ON cu.user_id = u.id
            WHERE lower(cu.email) = lower(:email)
            LIMIT 1
        """), {"email": body.email.strip()}).mappings().first()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if str(row.get("role") or "").strip().lower() == "player":
            raise HTTPException(status_code=403, detail="Not an agent")

        if row.get("user_is_active") is False or row.get("auth_is_active") is False:
            raise HTTPException(status_code=403, detail="Agent is inactive")

        password_hash = row.get("password_hash")
        if not password_hash:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not pwd.verify(body.password, password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        return {
            "ok": True,
            "agent": {
                "id": row["id"],
                "email": row["email"],
                "role": row["role"],
            }
        }

from passlib.context import CryptContext
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

class AgentChangePasswordBody(BaseModel):
    email: str
    current_password: str
    new_password: str

@router.post("/agent/auth/change-password")
def change_password(body: AgentChangePasswordBody):
    email = body.email.strip().lower()
    current_password = body.current_password
    new_password = body.new_password.strip()

    if not email or not current_password or not new_password:
        raise HTTPException(status_code=400, detail="Missing fields")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT u.id, u.role, cu.password_hash
            FROM users u
            INNER JOIN c2w_users cu ON cu.user_id = u.id
            WHERE lower(cu.email) = :email
            LIMIT 1
        """), {"email": email}).mappings().first()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if str(row.get("role") or "").strip().lower() == "player":
            raise HTTPException(status_code=403, detail="Not an agent")

        if not pwd.verify(current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        conn.execute(text("""
            UPDATE c2w_users
            SET password_hash = :password_hash
            WHERE user_id = :user_id
        """), {
            "password_hash": pwd.hash(new_password),
            "user_id": row["id"],
        })

    return {"ok": True, "message": "Password updated"}
