import os
import json
import time
import hashlib
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import create_engine, text

load_dotenv("/var/www/coin2win/.env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in .env")

AUTH_SECRET = str(os.getenv("AUTH_SECRET") or "").strip()
AUTH_ALGORITHM = "HS256"
AGENT_AUTH_EXPIRE_HOURS = int(
    os.getenv("AGENT_AUTH_EXPIRE_HOURS")
    or os.getenv("AUTH_EXPIRE_HOURS")
    or "72"
)

if not AUTH_SECRET:
    raise RuntimeError("Missing AUTH_SECRET in .env")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

router = APIRouter()

RATE_LIMIT = defaultdict(list)


def _request_ip(request: Request) -> str:
    try:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
    except Exception:
        pass
    return "unknown"


def _device_hash(ip: str, user_agent: str) -> str:
    raw = f"{ip}|{user_agent}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def _safe_login_device_log(
    *,
    request: Request,
    account_type: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    login_identifier: str | None = None,
    success: bool = False,
    failure_reason: str | None = None,
    metadata: dict | None = None,
):
    try:
        ip = _request_ip(request)
        ua = request.headers.get("user-agent", "")
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO login_device_logs (
                    account_type, user_id, agent_id, login_identifier,
                    success, failure_reason, ip_address, user_agent,
                    device_hash, request_path, metadata
                ) VALUES (
                    :account_type, :user_id, :agent_id, :login_identifier,
                    :success, :failure_reason, :ip_address, :user_agent,
                    :device_hash, :request_path, CAST(:metadata AS jsonb)
                )
            """), {
                "account_type": account_type,
                "user_id": user_id,
                "agent_id": agent_id,
                "login_identifier": login_identifier,
                "success": bool(success),
                "failure_reason": failure_reason,
                "ip_address": ip,
                "user_agent": ua,
                "device_hash": _device_hash(ip, ua),
                "request_path": str(request.url.path),
                "metadata": json.dumps(metadata or {}),
            })
    except Exception as e:
        print(f"[login-device-log-failed] account_type={account_type} success={success} error={e}", flush=True)


def check_rate_limit(ip: str, limit=5, window=60):
    now = time.time()
    RATE_LIMIT[ip] = [r for r in RATE_LIMIT[ip] if now - r < window]
    if len(RATE_LIMIT[ip]) >= limit:
        return False
    RATE_LIMIT[ip].append(now)
    return True

def get_agent_bearer_token(authorization: str | None) -> str:
    value = str(authorization or "").strip()

    if not value:
        raise HTTPException(
            status_code=401,
            detail="Missing agent Authorization header",
        )

    if not value.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid agent Authorization header",
        )

    token = value.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing agent bearer token",
        )

    return token


def create_agent_access_token(
    *,
    agent_id: str,
    email: str,
    role: str,
) -> str:
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(agent_id),
        "email": str(email),
        "role": str(role),
        "account_type": "agent",
        "iat": now,
        "exp": now + timedelta(hours=AGENT_AUTH_EXPIRE_HOURS),
    }

    return jwt.encode(
        payload,
        AUTH_SECRET,
        algorithm=AUTH_ALGORITHM,
    )


def decode_agent_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            AUTH_SECRET,
            algorithms=[AUTH_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired agent session",
        )

    if str(payload.get("account_type") or "") != "agent":
        raise HTTPException(
            status_code=401,
            detail="Invalid agent session",
        )

    agent_id = str(payload.get("sub") or "").strip()
    if not agent_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid agent session",
        )

    return payload


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

        login_email = body.email.strip().lower()

        if not row:
            _safe_login_device_log(request=request, account_type="agent", login_identifier=login_email, success=False, failure_reason="invalid_email")
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if str(row.get("role") or "").strip().lower() == "player":
            _safe_login_device_log(request=request, account_type="agent", user_id=row["id"], agent_id=row["id"], login_identifier=login_email, success=False, failure_reason="player_not_agent")
            raise HTTPException(status_code=403, detail="Not an agent")

        if row.get("user_is_active") is False or row.get("auth_is_active") is False:
            _safe_login_device_log(request=request, account_type="agent", user_id=row["id"], agent_id=row["id"], login_identifier=login_email, success=False, failure_reason="inactive")
            raise HTTPException(status_code=403, detail="Agent is inactive")

        password_hash = row.get("password_hash")
        if not password_hash:
            _safe_login_device_log(request=request, account_type="agent", user_id=row["id"], agent_id=row["id"], login_identifier=login_email, success=False, failure_reason="missing_password_hash")
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not pwd.verify(body.password, password_hash):
            _safe_login_device_log(request=request, account_type="agent", user_id=row["id"], agent_id=row["id"], login_identifier=login_email, success=False, failure_reason="invalid_password")
            raise HTTPException(status_code=401, detail="Invalid credentials")

        _safe_login_device_log(
            request=request,
            account_type="agent",
            user_id=row["id"],
            agent_id=row["id"],
            login_identifier=login_email,
            success=True,
            metadata={"role": row["role"]},
        )

        agent_token = create_agent_access_token(
            agent_id=row["id"],
            email=row["email"],
            role=row["role"],
        )

        return {
            "ok": True,
            "agent_token": agent_token,
            "expires_in_hours": AGENT_AUTH_EXPIRE_HOURS,
            "agent": {
                "id": row["id"],
                "email": row["email"],
                "role": row["role"],
            },
        }

from passlib.context import CryptContext
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

class AgentChangePasswordBody(BaseModel):
    new_password: str

@router.post("/agent/auth/change-password")
def change_password(body: AgentChangePasswordBody, request: Request):
    authorization = request.headers.get("authorization")
    token = get_agent_bearer_token(authorization)
    payload = decode_agent_access_token(token)

    agent_id = str(payload.get("sub") or "").strip()
    new_password = body.new_password.strip()

    if not agent_id or not new_password:
        raise HTTPException(status_code=400, detail="Missing fields")

    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT
                u.id,
                u.role,
                COALESCE(u.is_active, TRUE) AS user_is_active,
                COALESCE(cu.is_active, TRUE) AS auth_is_active
            FROM users u
            INNER JOIN c2w_users cu ON cu.user_id = u.id
            WHERE u.id = :agent_id
            LIMIT 1
        """), {"agent_id": agent_id}).mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")

        if str(row.get("role") or "").strip().lower() == "player":
            raise HTTPException(status_code=403, detail="Not an agent")

        if not bool(row["user_is_active"]) or not bool(row["auth_is_active"]):
            raise HTTPException(status_code=403, detail="Agent account is inactive")

        conn.execute(text("""
            UPDATE c2w_users
            SET password_hash = :password_hash
            WHERE user_id = :user_id
        """), {
            "password_hash": pwd.hash(new_password),
            "user_id": row["id"],
        })

    return {"ok": True, "agent_id": agent_id, "message": "Password updated"}
