import os
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text

load_dotenv("/var/www/coin2win/.env")

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

router = APIRouter()

@router.post("/agent/change-password")
def change_password(body: dict):
    user_id = body.get("user_id")
    new_password = body.get("new_password")

    if not user_id or not new_password:
        raise HTTPException(status_code=400, detail="Missing fields")

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET password_hash = crypt(:p, gen_salt('bf')) WHERE id = :u"),
            {"p": new_password, "u": user_id}
        )

    return {"ok": True}
