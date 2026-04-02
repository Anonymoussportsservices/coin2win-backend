from fastapi import HTTPException
from app.core.config import ADMIN_KEY

def require_admin(x_admin_key: str | None):

    if not ADMIN_KEY:
        return

    if not x_admin_key or x_admin_key.strip() != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
