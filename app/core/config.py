import os
from dotenv import load_dotenv

ENV_PATH = os.getenv("COIN2WIN_ENV_PATH", "/var/www/coin2win/.env")
load_dotenv(ENV_PATH)

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
NOWPAYMENTS_IPN_CALLBACK_URL = os.getenv("NOWPAYMENTS_IPN_CALLBACK_URL")

MIN_DEPOSIT_USD = float(os.getenv("MIN_DEPOSIT_USD", "20"))
MIN_WITHDRAW_USD = float(os.getenv("MIN_WITHDRAW_USD", "20"))

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_KEY = (os.getenv("ADMIN_KEY") or "").strip()

ACTIVE_WITHDRAW_STATUSES = ("requested", "approved", "sent")

def validate_required():
    missing = []

    if not NOWPAYMENTS_API_KEY:
        missing.append("NOWPAYMENTS_API_KEY")

    if not NOWPAYMENTS_IPN_SECRET:
        missing.append("NOWPAYMENTS_IPN_SECRET")

    if not NOWPAYMENTS_IPN_CALLBACK_URL:
        missing.append("NOWPAYMENTS_IPN_CALLBACK_URL")

    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
