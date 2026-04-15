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

@router.post("/agent/change-password")
def change_password(body: dict):
    user_id = body.get("user_id")
    new_password = body.get("new_password")

    if not user_id or not new_password:
        raise HTTPException(status_code=400, detail="Missing fields")

    hashed = pwd.hash(new_password)

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE c2w_users
            SET password_hash = :h
            WHERE user_id = :u
        """), {"h": hashed, "u": user_id})

    return {"ok": True}
