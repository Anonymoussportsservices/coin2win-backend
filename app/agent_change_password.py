import os

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

load_dotenv("/var/www/coin2win/.env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

router = APIRouter()

class ChangePasswordBody(BaseModel):
    email: str
    current_password: str
    new_password: str

@router.post("/agent/auth/change-password")
def change_password(body: ChangePasswordBody):
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT id, password_hash
            FROM users
            WHERE lower(email) = lower(:email)
            LIMIT 1
        """), {"email": body.email.strip()}).mappings().first()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        valid = conn.execute(text("""
            SELECT crypt(:password, :hash) = :hash
        """), {
            "password": body.current_password,
            "hash": row["password_hash"],
        }).scalar()

        if not valid:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        conn.execute(text("""
            UPDATE users
            SET password_hash = crypt(:new_password, gen_salt('bf'))
            WHERE id = :id
        """), {
            "new_password": body.new_password,
            "id": row["id"],
        })

        return {"ok": True, "message": "Password updated"}
