from fastapi import HTTPException
from app.core.config import ADMIN_KEY

def require_admin(x_admin_key: str | None):

    expected = (ADMIN_KEY or "").strip()
    received = (x_admin_key or "").strip()
    if not expected or not received or received != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
