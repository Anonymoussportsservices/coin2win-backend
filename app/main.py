import os
import hmac
import hashlib
import json
import time
import requests
import random
import time

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv("/var/www/coin2win/.env")

from app.auth_routes import auth_router, init_auth_tables, get_bearer_token, decode_token
from sqlalchemy import (
    text,
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Text,
    DateTime,
    func,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

import os
AUTO_WITHDRAW_ENABLED = os.getenv("AUTO_WITHDRAW_ENABLED", "true") == "true"
AUTO_WITHDRAW_THRESHOLD = float(os.getenv("AUTO_WITHDRAW_THRESHOLD", "200"))

app = FastAPI()
app.mount("/uploads", StaticFiles(directory="/var/www/coin2win/uploads"), name="uploads")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://159.203.121.161:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from collections import deque
import threading
from datetime import datetime, timezone

LIVE_CASINO_FEED = deque(maxlen=50)
LIVE_CASINO_FEED_LOCK = threading.Lock()

def _mask_user_id(user_id: str) -> str:
    s = str(user_id or "").strip()
    if len(s) <= 2:
        return s
    if len(s) <= 6:
        return s[:2] + "***"
    return s[:2] + "***" + s[-2:]

FAKE_FEED_ENABLED = True
def _push_live_feed_event(
    game: str,
    user_id: str,
    amount_usd: float,
    payout: float,
    multiplier: float | None = None,
    source: str = "real",
    provider: str = "coin2win",
):
    masked_user = _mask_user_id(user_id)
    game_key = str(game or "").lower()
    game_name = game_key.title() if game_key else "Game"
    stake = round(float(amount_usd), 2)
    payout_rounded = round(float(payout), 2)
    profit = round(payout_rounded - stake, 2)
    occurred_at = datetime.now(timezone.utc).isoformat()

    if multiplier is not None:
        multiplier_rounded = round(float(multiplier), 2)
        message = f"{masked_user} won ${payout_rounded:.2f} on {game_name} at {multiplier_rounded:.2f}x"
    else:
        multiplier_rounded = None
        message = f"{masked_user} won ${payout_rounded:.2f} on {game_name}"

    event = {
        "id": f"{game_key}:{user_id}:{datetime.now(timezone.utc).timestamp()}",
        "event_type": "win",
        "game_key": game_key,
        "game_name": game_name,
        "user_id": str(user_id),
        "display_user": masked_user,
        "stake": stake,
        "payout": payout_rounded,
        "profit": profit,
        "multiplier": multiplier_rounded,
        "provider": str(provider or "coin2win"),
        "source": str(source or "real"),
        "occurred_at": occurred_at,
        "message": message,
        "is_big_win": payout_rounded >= 100,
        "game": game_key,
        "user": masked_user,
        "amount_usd": stake,
        "created_at": occurred_at,
    }
    with LIVE_CASINO_FEED_LOCK:
        LIVE_CASINO_FEED.appendleft(event)

def _generate_fake_live_feed_event():
    games = ["crash", "dice"]
    game = random.choice(games)

    fake_users = [
        "player7841",
        "alex992",
        "mario17",
        "betking44",
        "lucky928",
        "cryptofox",
        "spinpro",
        "cashout77",
    ]

    user_id = random.choice(fake_users)
    amount_usd = round(random.uniform(1.0, 20.0), 2)

    if game == "crash":
        multiplier = round(random.uniform(1.40, 8.50), 2)
        payout = round(amount_usd * multiplier, 2)
    else:
        multiplier = round(random.uniform(1.20, 4.50), 2)
        payout = round(amount_usd * multiplier, 2)

    _push_live_feed_event(
        game=game,
        user_id=user_id,
        amount_usd=amount_usd,
        payout=payout,
        multiplier=multiplier,
        source="fake",
    )

# --------------------------
# Config
# --------------------------
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
IPN_CALLBACK_URL = os.getenv("NOWPAYMENTS_IPN_CALLBACK_URL")

MIN_DEPOSIT_USD = float(os.getenv("MIN_DEPOSIT_USD", "20"))
MIN_WITHDRAW_USD = float(os.getenv("MIN_WITHDRAW_USD", "20"))
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()

if not NOWPAYMENTS_API_KEY:
    raise RuntimeError("Missing NOWPAYMENTS_API_KEY in .env")
if not NOWPAYMENTS_IPN_SECRET:
    raise RuntimeError("Missing NOWPAYMENTS_IPN_SECRET in .env")
if not IPN_CALLBACK_URL:
    raise RuntimeError("Missing NOWPAYMENTS_IPN_CALLBACK_URL in .env")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in .env")

ACTIVE_WITHDRAW_STATUSES = ("requested", "approved", "sent")

# --------------------------
# Database
# --------------------------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String(64), primary_key=True)
    kyc_status = Column(String(20), nullable=False, default="unverified", server_default="unverified")
    kyc_level = Column(Integer, nullable=False, default=0, server_default="0")
    auto_withdraw_enabled = Column(Integer, nullable=False, default=1, server_default="1")
    auto_withdraw_limit = Column(Float, nullable=False, default=0, server_default="0")
    kyc_doc_front_path = Column(Text, nullable=True)
    kyc_selfie_path = Column(Text, nullable=True)
    kyc_poa_path = Column(Text, nullable=True)
    kyc_verified_at = Column(DateTime(timezone=True), nullable=True)
    kyc_rejected_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), unique=True, nullable=False)

    # New model:
    balance_total = Column(Float, default=0)
    balance_pending = Column(Float, default=0)
    balance_available = Column(Float, default=0)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Deposit(Base):
    __tablename__ = "deposits"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)

    payment_id = Column(String(64), unique=True, nullable=False)
    status = Column(String(32), default="waiting")

    amount_usd = Column(Float, nullable=False)
    pay_currency = Column(String(32), nullable=False)

    pay_amount = Column(Float, nullable=True)
    pay_address = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)

    amount_usd = Column(Float, nullable=False)
    payout_currency = Column(String(32), nullable=False)
    payout_address = Column(Text, nullable=False)

    # requested -> approved -> sent -> completed
    # rejected (refunds)
    status = Column(String(32), default="requested")
    refunded = Column(Integer, default=0)  # 0/1
    note = Column(Text, nullable=True)
    batch_withdrawal_id = Column(String(128), nullable=True)
    payout_status = Column(String(64), nullable=True)
    payout_response_json = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)

    # deposit, withdrawal_request, withdrawal_refund, withdrawal_complete, etc
    type = Column(String(64), nullable=False)

    # + credit, - debit
    amount = Column(Float, nullable=False)

    # Store wallet TOTAL after the transaction (or total at time of event)
    balance_after = Column(Float, nullable=False)

    reference = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

@app.on_event("startup")
def startup():
    init_auth_tables()
    

# ==========================
# GLOBAL CRASH V2 (24/7 shared rounds)
# ==========================

GLOBAL_CRASH_ENGINE_USER = "__global_crash_engine__"
GLOBAL_CRASH_BETTING_SECONDS = int(os.getenv("GLOBAL_CRASH_BETTING_SECONDS", "5"))
GLOBAL_CRASH_COOLDOWN_SECONDS = int(os.getenv("GLOBAL_CRASH_COOLDOWN_SECONDS", "2"))
GLOBAL_CRASH_TICK_SECONDS = float(os.getenv("GLOBAL_CRASH_TICK_SECONDS", "0.25"))

GLOBAL_CRASH_ENGINE_STARTED = False
def crash_global_round_bets(round_id: int):
    db = SessionLocal()
    try:
        rows = (
            db.query(GlobalCrashBet)
            .filter(GlobalCrashBet.round_id == round_id)
            .order_by(GlobalCrashBet.id.asc())
            .all()
        )

        def mask(uid: str):
            raw = str(uid or "player")
            if len(raw) <= 4:
                return raw[0] + "***" if raw else "p***"
            return raw[:1] + "***" + raw[-3:]

        return {
            "round_id": round_id,
            "count": len(rows),
            "bets": [
                {
                    "bet_id": r.id,
                    "player": mask(r.user_id),
                    "amount": float(r.amount_usd),
                    "auto_cashout": float(r.auto_cashout) if r.auto_cashout is not None else None,
                    "status": str(r.status),
                    "payout": float(r.payout),
                    "cashout_multiplier": float(r.cashout_multiplier) if r.cashout_multiplier is not None else None,
                    "is_real": True,
                }
                for r in rows
            ],
        }
    finally:
        db.close()

@app.get("/studio/live-wins")
def studio_live_wins(limit: int = 20):
    safe_limit = max(1, min(int(limit), 50))
    with LIVE_CASINO_FEED_LOCK:
        items = list(LIVE_CASINO_FEED)[:safe_limit]
    return {
        "ok": True,
        "items": items,
    }
@app.post("/studio/live-wins/seed")

def studio_live_wins_seed():
    if not FAKE_FEED_ENABLED:
        return {"ok": True, "seeded": False, "reason": "disabled"}

    with LIVE_CASINO_FEED_LOCK:
        latest = LIVE_CASINO_FEED[0] if LIVE_CASINO_FEED else None

    should_seed = True

    if latest and latest.get("created_at"):
        try:
            latest_dt = datetime.fromisoformat(latest["created_at"])
            age_seconds = (datetime.now(timezone.utc) - latest_dt).total_seconds()
            should_seed = age_seconds >= 12
        except Exception:
            should_seed = True

    if should_seed:
        _generate_fake_live_feed_event()
        return {"ok": True, "seeded": True}

    return {"ok": True, "seeded": False, "reason": "feed_recently_active"}

@app.get("/studio/crash-global/round-bets/{round_id}")
def crash_global_round_bets(round_id: int):
    db = SessionLocal()
    try:
        rows = (
            db.query(GlobalCrashBet)
            .filter(GlobalCrashBet.round_id == round_id)
            .order_by(GlobalCrashBet.id.asc())
            .all()
        )

        user_ids = list({str(r.user_id) for r in rows if r.user_id})
        username_map = {}

        if user_ids:
            result = db.execute(
                text("""
                    SELECT user_id, username
                    FROM c2w_users
                    WHERE user_id = ANY(:user_ids)
                """),
                {"user_ids": user_ids},
            ).mappings().all()

            username_map = {
                str(row["user_id"]): str(row["username"])
                for row in result
            }

        return {
            "round_id": round_id,
            "count": len(rows),
            "bets": [
                {
                    "bet_id": r.id,
                    "player": username_map.get(str(r.user_id), str(r.user_id)),
                    "amount": float(r.amount_usd),
                    "auto_cashout": float(r.auto_cashout) if r.auto_cashout is not None else None,
                    "status": str(r.status),
                    "payout": float(r.payout),
                    "cashout_multiplier": float(r.cashout_multiplier) if r.cashout_multiplier is not None else None,
                    "is_real": True,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


class GlobalCrashRound(Base):
    __tablename__ = "global_crash_rounds"

    id = Column(Integer, primary_key=True)

    server_seed_hash = Column(String(64), nullable=False)
    client_seed = Column(Text, nullable=False)
    nonce = Column(Integer, nullable=False)

    crash_point = Column(Float, nullable=False)

    # betting, running, crashed
    status = Column(String(32), nullable=False, default="betting")

    betting_started_at = Column(DateTime(timezone=True), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    crashed_at = Column(DateTime(timezone=True), nullable=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GlobalCrashBet(Base):
    __tablename__ = "global_crash_bets"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    round_id = Column(Integer, ForeignKey("global_crash_rounds.id"), nullable=False)

    amount_usd = Column(Float, nullable=False)
    auto_cashout = Column(Float, nullable=True)

    # active, cashed_out, lost
    status = Column(String(32), nullable=False, default="active")
    cashout_multiplier = Column(Float, nullable=True)
    payout = Column(Float, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    settled_at = Column(DateTime(timezone=True), nullable=True)


Base.metadata.create_all(bind=engine)

# --------------------------
# Helpers
# --------------------------
def _require_admin(x_admin_key: str | None):
    # If ADMIN_KEY is blank, admin endpoints are open (MVP).
    if not ADMIN_KEY:
        return
    if not x_admin_key or x_admin_key.strip() != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _np_headers():
    return {
        "Authorization": f"Bearer {NOWPAYMENTS_API_KEY}",
        "Content-Type": "application/json",
    }

def _round2(x: float) -> float:
    return round(float(x), 2)

def _get_or_create_user_and_wallet(db, user_id: str) -> Wallet:
    user = db.get(User, user_id)
    if not user:
        user = User(id=user_id)
        db.add(user)
        db.flush()

    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).one_or_none()
    if not wallet:
        wallet = Wallet(
            user_id=user_id,
            balance_total=0,
            balance_pending=0,
            balance_available=0,
        )
        db.add(wallet)
        db.flush()

    return wallet

def _serialize_wallet(w: Wallet):
    return {
        "user_id": w.user_id,
        "balance_total": float(w.balance_total),
        "balance_available": float(w.balance_available),
        "balance_pending": float(w.balance_pending),
        "updated_at": str(w.updated_at),
    }

def _serialize_withdrawal(w: Withdrawal):
    return {
        "id": w.id,
        "user_id": w.user_id,
        "amount_usd": float(w.amount_usd),
        "payout_currency": w.payout_currency,
        "payout_address": w.payout_address,
        "status": w.status,
        "refunded": bool(w.refunded),
        "note": w.note,
        "created_at": str(w.created_at),
        "updated_at": str(w.updated_at),
    }

def _serialize_deposit(d: Deposit):
    return {
        "id": d.id,
        "user_id": d.user_id,
        "payment_id": d.payment_id,
        "status": d.status,
        "amount_usd": float(d.amount_usd),
        "pay_currency": d.pay_currency,
        "pay_amount": d.pay_amount,
        "pay_address": d.pay_address,
        "created_at": str(d.created_at),
        "updated_at": str(d.updated_at),
    }

def _serialize_tx(t: Transaction):
    return {
        "id": t.id,
        "user_id": t.user_id,
        "type": t.type,
        "amount": float(t.amount),
        "balance_after": float(t.balance_after),
        "reference": t.reference,
        "created_at": str(t.created_at),
    }

# --------------------------
# Public Routes
# --------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
    <head><title>Coin2Win</title></head>
    <body style="background:#0f172a;color:white;text-align:center;padding-top:15%">
        <h1>Coin2Win.bet</h1>
        <p>Crypto Casino Platform</p>
        <p>Coming Online 🚀</p>
        <p><a style="color:#60a5fa" href="/docs">API Docs</a></p>
    </body>
    </html>
    """

@app.get("/health")
def health():
    return {"status": "ok", "app": "coin2win"}

@app.get("/wallet/{user_id}")
def wallet(user_id: str):
    db = SessionLocal()
    try:
        w = _get_or_create_user_and_wallet(db, user_id)
        db.commit()
        return _serialize_wallet(w)
    finally:
        db.close()
#---------------------------------------------------
#quiet-feed seeder
#--------------------------------------------------


# --------------------------
# Deposits
# --------------------------
@app.post("/deposit/create")
async def create_deposit(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or empty JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    user_id = str(body.get("user_id", "")).strip()
    pay_currency = str(body.get("pay_currency", "")).lower().strip()

    try:
        amount_usd = float(body.get("amount_usd", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount_usd must be a valid number")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    if amount_usd < MIN_DEPOSIT_USD:
        raise HTTPException(status_code=400, detail=f"Minimum deposit is ${MIN_DEPOSIT_USD:.0f} USD")
    if not pay_currency:
        raise HTTPException(status_code=400, detail="pay_currency required")

    payload = {
        "price_amount": amount_usd,
        "price_currency": "usd",
        "pay_currency": pay_currency,
        "ipn_callback_url": IPN_CALLBACK_URL,
        "order_id": f"{user_id}_{int(time.time())}",
        "order_description": "Coin2Win deposit",
    }

    try:
        r = requests.post(
            "https://api.nowpayments.io/v1/payment",
            headers=_np_headers(),
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Deposit provider request failed: {str(e)}")

    try:
        data = r.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail=f"Deposit provider returned non-JSON response (HTTP {r.status_code})",
        )

    if r.status_code >= 400:
        provider_error = (
            data.get("message")
            or data.get("error")
            or data.get("detail")
            or f"Deposit provider error (HTTP {r.status_code})"
        )
        raise HTTPException(status_code=502, detail=str(provider_error))

    payment_id = str(data.get("payment_id", "")).strip()
    payment_status = str(data.get("payment_status") or "waiting").lower().strip()

    if not payment_id:
        raise HTTPException(status_code=502, detail="Deposit provider did not return payment_id")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        dep = Deposit(

            user_id=user_id,
            payment_id=payment_id,
            status=payment_status or "waiting",
            amount_usd=float(amount_usd),
            pay_currency=pay_currency,
            pay_amount=float(data.get("pay_amount")) if data.get("pay_amount") is not None else None,
            pay_address=data.get("pay_address"),
        )
        db.add(dep)
        db.commit()

        return {
            "ok": True,
            "payment_id": dep.payment_id,
            "status": dep.status,
            "price_amount_usd": amount_usd,
            "pay_currency": pay_currency,
            "pay_amount": data.get("pay_amount"),
            "pay_address": data.get("pay_address"),
            "payment_url": data.get("payment_url"),
        }
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Deposit already exists for this payment_id")
    finally:
        db.close()


@app.post("/webhooks/nowpayments")
async def webhook(request: Request):
    raw = await request.body()

    if not raw:
        raise HTTPException(status_code=400, detail="Empty webhook body")

    sig = request.headers.get("x-nowpayments-sig")
    if not sig:
        raise HTTPException(status_code=400, detail="Missing x-nowpayments-sig")

    gen = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode(),
        raw,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(sig, gen):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid webhook JSON")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Webhook JSON must be an object")

    payment_id = str(data.get("payment_id", "")).strip()
    status = str(data.get("payment_status", "")).lower().strip()

    if not payment_id:
        raise HTTPException(status_code=400, detail="Missing payment_id")

    db = SessionLocal()
    try:
        dep = db.query(Deposit).filter(Deposit.payment_id == payment_id).one_or_none()
        if not dep:
            raise HTTPException(status_code=400, detail="Unknown payment_id")

        previous_status = str(dep.status or "").lower().strip()

        # Track lifecycle statuses even before final credit
        if status and previous_status != "credited" and status != previous_status:
            dep.status = status

        # Credit exactly once when finished
        if status == "finished" and previous_status != "credited":
            _get_or_create_user_and_wallet(db, dep.user_id)

            w = db.query(Wallet).filter(Wallet.user_id == dep.user_id).with_for_update().one()
            w.balance_total = _round2(w.balance_total + dep.amount_usd)
            w.balance_available = _round2(w.balance_available + dep.amount_usd)

            tx = Transaction(
                user_id=dep.user_id,
                type="deposit",
                amount=float(dep.amount_usd),
                balance_after=float(w.balance_total),
                reference=(
    f"{payment_id}|partial"
    if status in ["confirmed", "partially_paid"]
    else payment_id
),
            )
            db.add(tx)
            dep.status = "credited"

        db.commit()
        return {"ok": True, "payment_id": payment_id, "status": dep.status}
    finally:
        db.close()

# --------------------------
# Withdrawals
# --------------------------
@app.post("/withdraw/create")
async def withdraw_create(request: Request):
    body = await request.json()

    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))
    currency = str(body.get("payout_currency", "")).lower().strip()
    address = str(body.get("payout_address", "")).strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    if amount < MIN_WITHDRAW_USD:
        raise HTTPException(status_code=400, detail=f"Minimum withdrawal is ${MIN_WITHDRAW_USD:.0f}")
    if not currency:
        raise HTTPException(status_code=400, detail="payout_currency required")
    if not address:
        raise HTTPException(status_code=400, detail="payout_address required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        # KYC / HYBRID WITHDRAW CHECK
        user = db.query(User).filter(User.id == user_id).one()

        user_kyc_level = int(getattr(user, "kyc_level", 0) or 0)
        user_auto_withdraw_enabled = bool(getattr(user, "auto_withdraw_enabled", True))
        user_auto_withdraw_limit = float(getattr(user, "auto_withdraw_limit", 0) or 0)

        # fallback defaults by tier if limit not set
        if user_auto_withdraw_limit <= 0:
            if user_kyc_level >= 2:
                user_auto_withdraw_limit = 1000.0
            elif user_kyc_level == 1:
                user_auto_withdraw_limit = 200.0
            else:
                user_auto_withdraw_limit = 0.0

        auto_approve_withdraw = (
            AUTO_WITHDRAW_ENABLED
            and user_auto_withdraw_enabled
            and user_kyc_level >= 1
            and amount <= user_auto_withdraw_limit
        )

        # 🔒 Withdrawal lock: only one active withdrawal at a time
        existing_active = (
            db.query(Withdrawal)
            .filter(Withdrawal.user_id == user_id)
            .filter(Withdrawal.status.in_(ACTIVE_WITHDRAW_STATUSES))
            .count()
        )
        if existing_active > 0:
            raise HTTPException(
                status_code=400,
                detail="You already have a withdrawal in progress. Please wait until it is completed or rejected.",
            )

        # Lock wallet row
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if float(w.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        # Move funds: available -> pending
        w.balance_available = _round2(w.balance_available - amount)
        w.balance_pending = _round2(w.balance_pending + amount)

        wd = Withdrawal(
            user_id=user_id,
            amount_usd=float(amount),
            payout_currency=currency,
            payout_address=address,
            status="approved" if auto_approve_withdraw else "requested",
        )
        db.add(wd)
        db.flush()  # get wd.id

        tx = Transaction(
            user_id=user_id,
            type="withdrawal_request",
            amount=-float(amount),
            balance_after=float(w.balance_total),  # total unchanged at request time
            reference=f"withdrawal:{wd.id}",
        )
        db.add(tx)

        db.commit()
        with engine.begin() as conn:
            _insert_kyc_audit_log(conn, user.id, "approve_kyc", actor="admin", from_level=prev_level, to_level=int(user.kyc_level or 0), note=f"Approved level {int(user.kyc_level or 0)}")
        return {
            "ok": True,
            "withdrawal_id": wd.id,
            "status": wd.status,
            "amount_usd": wd.amount_usd,
            "payout_currency": wd.payout_currency,
        }
    finally:
        db.close()

@app.get("/withdraw/{user_id}")
def withdraw_list_user(user_id: str, limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(Withdrawal)
            .filter(Withdrawal.user_id == user_id)
            .order_by(Withdrawal.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {"user_id": user_id, "count": len(rows), "withdrawals": [_serialize_withdrawal(w) for w in rows]}
    finally:
        db.close()

def _refund_withdrawal(db, wd: Withdrawal, reason: str):
    # Refund only once
    if wd.refunded == 1:
        return

    w = db.query(Wallet).filter(Wallet.user_id == wd.user_id).with_for_update().one()

    # Move funds back: pending -> available
    w.balance_pending = _round2(w.balance_pending - wd.amount_usd)
    w.balance_available = _round2(w.balance_available + wd.amount_usd)

    tx = Transaction(
        user_id=wd.user_id,
        type="withdrawal_refund",
        amount=float(wd.amount_usd),
        balance_after=float(w.balance_total),  # total unchanged on refund
        reference=f"withdrawal:{wd.id}",
    )
    db.add(tx)

    wd.refunded = 1
    wd.status = "rejected"
    wd.note = reason

# --------------------------
# Admin Withdrawals
# --------------------------
@app.get("/admin/withdrawals")
def admin_withdrawals(limit: int = 50, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        rows = (
            db.query(Withdrawal)
            .order_by(Withdrawal.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {"count": len(rows), "withdrawals": [_serialize_withdrawal(w) for w in rows]}
    finally:
        db.close()

@app.get("/admin/withdrawals/{withdrawal_id}")
def admin_withdrawal_get(withdrawal_id: int, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        return _serialize_withdrawal(w)
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def admin_withdrawal_approve(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""

    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if w.status in ("completed", "rejected"):
            raise HTTPException(status_code=400, detail=f"Cannot approve withdrawal in status '{w.status}'")

        w.status = "approved"
        if note:
            w.note = note

        db.commit()
        return {"ok": True, "withdrawal_id": w.id, "status": w.status}
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/mark_sent")
async def admin_withdrawal_mark_sent(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""

    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if w.status not in ("approved", "requested"):
            raise HTTPException(status_code=400, detail=f"Cannot mark sent from status '{w.status}'")

        w.status = "sent"
        if note:
            w.note = note

        db.commit()
        return {"ok": True, "withdrawal_id": w.id, "status": w.status}
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/complete")
async def admin_withdrawal_complete(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """
    FINALIZE payout:
    pending -> reduced
    total   -> reduced
    """
    _require_admin(x_admin_key)
    body = await request.json()
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status == "rejected":
            raise HTTPException(status_code=400, detail="Cannot complete a rejected withdrawal")
        if wd.status == "completed":
            return {"ok": True, "withdrawal_id": wd.id, "status": "completed"}

        _get_or_create_user_and_wallet(db, wd.user_id)
        w = db.query(Wallet).filter(Wallet.user_id == wd.user_id).with_for_update().one()

        w.balance_pending = _round2(w.balance_pending - wd.amount_usd)
        w.balance_total = _round2(w.balance_total - wd.amount_usd)

        if w.balance_pending < -0.000001:
            raise HTTPException(status_code=500, detail="Wallet pending balance went negative (data mismatch)")

        tx = Transaction(
            user_id=wd.user_id,
            type="withdrawal_complete",
            amount=-float(wd.amount_usd),
            balance_after=float(w.balance_total),
            reference=f"withdrawal:{wd.id}",
        )
        db.add(tx)

        wd.status = "completed"
        if note:
            wd.note = note

        db.commit()
        return {"ok": True, "withdrawal_id": wd.id, "status": wd.status, "balance_total": float(w.balance_total)}
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/reject")
async def admin_withdrawal_reject(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """
    Reject + refund:
    pending -> available
    total unchanged
    """
    _require_admin(x_admin_key)
    body = await request.json()
    reason = str(body.get("reason", "")).strip() if isinstance(body, dict) else ""
    if not reason:
        reason = "Withdrawal rejected by support"

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status == "completed":
            raise HTTPException(status_code=400, detail="Cannot reject a completed withdrawal")

        _get_or_create_user_and_wallet(db, wd.user_id)
        _refund_withdrawal(db, wd, reason=reason)

        db.commit()
        return {"ok": True, "withdrawal_id": wd.id, "status": wd.status, "refunded": bool(wd.refunded)}
    finally:
        db.close()

# --------------------------
# Admin Deposits
# --------------------------

@app.get("/deposit/{user_id}")
def deposit_list_user(user_id: str, limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(Deposit)
            .filter(Deposit.user_id == user_id)
            .order_by(Deposit.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "user_id": user_id,
            "count": len(rows),
            "deposits": [_serialize_deposit(d) for d in rows],
        }
    finally:
        db.close()


@app.get("/admin/deposits")
def admin_deposits(limit: int = 50, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        rows = (
            db.query(Deposit)
            .order_by(Deposit.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {"count": len(rows), "deposits": [_serialize_deposit(d) for d in rows]}
    finally:
        db.close()


@app.get("/admin/deposits/{payment_id}")
def admin_deposit_by_payment_id(payment_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        dep = db.query(Deposit).filter(Deposit.payment_id == payment_id).one_or_none()
        if not dep:
            raise HTTPException(status_code=404, detail="Deposit not found")
        return _serialize_deposit(dep)
    finally:
        db.close()

@app.get("/admin/deposits/user/{user_id}")
def admin_user_deposits(user_id: str, limit: int = 50, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        rows = (
            db.query(Deposit)
            .filter(Deposit.user_id == user_id)
            .order_by(Deposit.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {"count": len(rows), "deposits": [_serialize_deposit(d) for d in rows]}
    finally:
        db.close()


@app.get("/admin/deposits/scoped/{viewer_id}")
def admin_scoped_deposits(
    viewer_id: str,
    limit: int = 200,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        viewer = _get_user_hierarchy_row(db, viewer_id)
        if not viewer:
            raise HTTPException(status_code=404, detail="Viewer not found")

        viewer_role = str(viewer.get("role") or "").strip().lower()
        if not _can_view_admin_hierarchy(viewer_role):
            raise HTTPException(status_code=403, detail="Hierarchy access not allowed for this role")

        if viewer_role in {"super_admin", "admin"}:
            rows = (
                db.query(Deposit)
                .order_by(Deposit.id.desc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return {
                "ok": True,
                "viewer_id": viewer_id,
                "viewer_role": viewer_role,
                "scope": "global",
                "count": len(rows),
                "deposits": [_serialize_deposit(d) for d in rows],
            }

        rows = db.execute(sa_text("""
            WITH RECURSIVE user_tree AS (
                SELECT id
                FROM users
                WHERE id = :viewer_id

                UNION ALL

                SELECT u.id
                FROM users u
                INNER JOIN user_tree ut ON u.parent_id = ut.id
            )
            SELECT
                d.id,
                d.user_id,
                d.payment_id,
                d.status,
                d.amount_usd,
                d.pay_currency,
                d.pay_amount,
                d.pay_address,
                d.created_at,
                d.updated_at
            FROM deposits d
            INNER JOIN user_tree ut ON d.user_id = ut.id
            ORDER BY d.id DESC
            LIMIT :limit
        """), {
            "viewer_id": viewer_id,
            "limit": max(1, min(limit, 500)),
        }).fetchall()

        deposits = [dict(r._mapping) for r in rows]

        for d in deposits:
            if d.get("amount_usd") is not None:
                d["amount_usd"] = float(d["amount_usd"])
            if d.get("pay_amount") is not None:
                d["pay_amount"] = float(d["pay_amount"])
            if d.get("created_at") is not None:
                d["created_at"] = str(d["created_at"])
            if d.get("updated_at") is not None:
                d["updated_at"] = str(d["updated_at"])

        return {
            "ok": True,
            "viewer_id": viewer_id,
            "viewer_role": viewer_role,
            "scope": "subtree",
            "count": len(deposits),
            "deposits": deposits,
        }
    finally:
        db.close()


# --------------------------
# Admin Transactions
# --------------------------

@app.get("/admin/transactions")
def admin_transactions(limit: int = 100, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .order_by(Transaction.id.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return {"count": len(rows), "transactions": [_serialize_tx(t) for t in rows]}
    finally:
        db.close()

@app.get("/admin/transactions/user/{user_id}")
def admin_transactions_user(user_id: str, limit: int = 100, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .order_by(Transaction.id.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return {"user_id": user_id, "count": len(rows), "transactions": [_serialize_tx(t) for t in rows]}
    finally:
        db.close()

@app.get("/admin/users/{user_id}/snapshot")
def admin_user_snapshot(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        w = db.query(Wallet).filter(Wallet.user_id == user_id).one()
        deposits = (
            db.query(Deposit)
            .filter(Deposit.user_id == user_id)
            .order_by(Deposit.id.desc())
            .limit(10)
            .all()
        )
        withdrawals = (
            db.query(Withdrawal)
            .filter(Withdrawal.user_id == user_id)
            .order_by(Withdrawal.id.desc())
            .limit(10)
            .all()
        )
        txs = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .order_by(Transaction.id.desc())
            .limit(20)
            .all()
        )

        active_withdrawals = [
            _serialize_withdrawal(x)
            for x in withdrawals
            if x.status in ACTIVE_WITHDRAW_STATUSES
        ]

        return {
            "user_id": user_id,
            "wallet": _serialize_wallet(w),
            "active_withdrawals": active_withdrawals,
            "deposits_last_10": [_serialize_deposit(d) for d in deposits],
            "withdrawals_last_10": [_serialize_withdrawal(wd) for wd in withdrawals],
            "transactions_last_20": [_serialize_tx(t) for t in txs],
        }
    finally:
        db.close()


@app.get("/transactions/me")
def my_transactions(
    limit: int = 100,
    start_date: str | None = None,
    end_date: str | None = None,
    authorization: str | None = Header(default=None),
):
    token = get_bearer_token(authorization)
    payload = decode_token(token)

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    db = SessionLocal()
    try:
        query = db.query(Transaction).filter(Transaction.user_id == user_id)

        if start_date:
            start_dt = datetime.fromisoformat(f"{start_date}T00:00:00")
            query = query.filter(Transaction.created_at >= start_dt)

        if end_date:
            end_dt = datetime.fromisoformat(f"{end_date}T23:59:59.999999")
            query = query.filter(Transaction.created_at <= end_dt)

        rows = (
            query.order_by(Transaction.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )

        return {
            "count": len(rows),
            "transactions": [
                {
                    "id": t.id,
                    "user_id": t.user_id,
                    "type": t.type,
                    "amount": float(t.amount) if t.amount is not None else None,
                    "balance_after": float(t.balance_after) if t.balance_after is not None else None,
                    "reference": t.reference,
                    "created_at": str(t.created_at) if t.created_at else None,
                }
                for t in rows
            ],
        }
    finally:
        db.close()


# ==========================
# STUDIO: DICE (Option A - Instant)
# ==========================
import secrets
import math
from sqlalchemy import Boolean

HOUSE_EDGE = float(os.getenv("DICE_HOUSE_EDGE", "1.0"))  # percent, e.g. 1.0 = 1%
DICE_AUTO_ROTATE_AFTER = 50
GLOBAL_CRASH_AUTO_ROTATE_AFTER = 50

class DiceSeed(Base):
    __tablename__ = "dice_seeds"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), unique=True, nullable=False)

    server_seed = Column(Text, nullable=False)         # keep secret until rotated
    server_seed_hash = Column(String(64), nullable=False)

    client_seed = Column(Text, nullable=False, default="client_default")
    nonce = Column(Integer, nullable=False, default=0)

    # store previous server seed so we can reveal it after rotation (provably fair)
    prev_server_seed = Column(Text, nullable=True)
    prev_server_seed_hash = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class DiceBet(Base):
    __tablename__ = "dice_bets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)

    amount_usd = Column(Float, nullable=False)

    # under / over
    condition = Column(String(8), nullable=False)
    target = Column(Float, nullable=False)

    # results
    roll = Column(Float, nullable=False)
    win = Column(Boolean, nullable=False, default=False)
    multiplier = Column(Float, nullable=False, default=0)
    payout = Column(Float, nullable=False, default=0)

    # fairness proof
    server_seed_hash = Column(String(64), nullable=False)
    client_seed = Column(Text, nullable=False)
    nonce = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def _get_or_create_dice_seed(db, user_id: str) -> DiceSeed:
    row = db.query(DiceSeed).filter(DiceSeed.user_id == user_id).one_or_none()
    if row:
        return row

    server_seed = secrets.token_hex(32)
    row = DiceSeed(
        user_id=user_id,
        server_seed=server_seed,
        server_seed_hash=_sha256_hex(server_seed),
        client_seed="client_default",
        nonce=0,
    )
    db.add(row)
    db.flush()
    return row

def _dice_roll(server_seed: str, client_seed: str, nonce: int) -> float:
    # Deterministic roll in [0.00, 99.99]
    msg = f"{client_seed}:{nonce}"
    h = hmac.new(server_seed.encode(), msg.encode(), hashlib.sha256).hexdigest()
    # take 52-ish bits and map to [0,1)
    n = int(h[:13], 16)
    r01 = n / float(2**52)
    roll = math.floor(r01 * 10000) / 100.0  # 2 decimals
    if roll >= 100:
        roll = 99.99
    return roll

def _dice_multiplier(chance: float) -> float:
    # multiplier = (100 - house_edge) / chance
    effective = max(0.0, 100.0 - HOUSE_EDGE)
    return effective / chance

@app.get("/studio/dice/seed/{user_id}")
def dice_get_seed(user_id: str):
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        s = _get_or_create_dice_seed(db, user_id)
        db.commit()
        return {
            "user_id": user_id,
            "server_seed_hash": s.server_seed_hash,
            "client_seed": s.client_seed,
            "nonce_next": s.nonce + 1,
            "house_edge_percent": HOUSE_EDGE,
        }
    finally:
        db.close()

@app.post("/studio/dice/seed/{user_id}/client")
async def dice_set_client_seed(user_id: str, request: Request):
    body = await request.json()
    client_seed = str(body.get("client_seed", "")).strip()
    if not client_seed:
        raise HTTPException(status_code=400, detail="client_seed required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        s = _get_or_create_dice_seed(db, user_id)
        s.client_seed = client_seed
        db.commit()
        return {"ok": True, "user_id": user_id, "client_seed": s.client_seed}
    finally:
        db.close()

@app.post("/studio/dice/seed/{user_id}/rotate")
def dice_rotate_server_seed(user_id: str):
    """
    Rotates server seed and REVEALS the previous one (provably fair).
    """
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        s = db.query(DiceSeed).filter(DiceSeed.user_id == user_id).with_for_update().one_or_none()
        if not s:
            s = _get_or_create_dice_seed(db, user_id)

        # move current -> prev (revealable)
        s.prev_server_seed = s.server_seed
        s.prev_server_seed_hash = s.server_seed_hash

        # generate new server seed
        new_seed = secrets.token_hex(32)
        s.server_seed = new_seed
        s.server_seed_hash = _sha256_hex(new_seed)
        s.nonce = 0

        db.commit()
        return {
            "ok": True,
            "user_id": user_id,
            "new_server_seed_hash": s.server_seed_hash,
            "revealed_prev_server_seed": s.prev_server_seed,
            "revealed_prev_server_seed_hash": s.prev_server_seed_hash,
            "client_seed": s.client_seed,
            "nonce_reset_to": 0,
        }
    finally:
        db.close()

@app.post("/studio/dice/bet")
async def dice_bet(request: Request):
    """
    Instant dice bet.
    Body:
      user_id, amount_usd, condition ("under"|"over"), target (float), optional client_seed
    """
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))
    condition = str(body.get("condition", "")).lower().strip()
    target = float(body.get("target", 0))
    client_seed_override = str(body.get("client_seed", "")).strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    if condition not in ("under", "over"):
        raise HTTPException(status_code=400, detail="condition must be 'under' or 'over'")
    if target <= 0 or target >= 100:
        raise HTTPException(status_code=400, detail="target must be between 0 and 100 (exclusive)")

    # Chance and win logic
    if condition == "under":
        chance = target
    else:
        chance = 100.0 - target

    if chance <= 0:
        raise HTTPException(status_code=400, detail="Invalid chance")

    multiplier = _dice_multiplier(chance)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        _get_or_create_dice_seed(db, user_id)

        # Lock wallet and seed row to keep nonce + balances consistent
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()
        s = db.query(DiceSeed).filter(DiceSeed.user_id == user_id).with_for_update().one()

        if client_seed_override:
            s.client_seed = client_seed_override

        if float(w.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        # Debit immediately (instant game)
        w.balance_available = _round2(w.balance_available - amount)
        w.balance_total = _round2(w.balance_total - amount)

        tx1 = Transaction(
            user_id=user_id,
            type="dice_bet",
            amount=-float(amount),
            balance_after=float(w.balance_total),
            reference=None,
        )
        db.add(tx1)

        # increment nonce and roll
        s.nonce += 1
        roll = _dice_roll(s.server_seed, s.client_seed, s.nonce)

        if condition == "under":
            win = roll < target
        else:
            win = roll > target

        payout = 0.0
        if win:
            payout = _round2(amount * multiplier)
            w.balance_total = _round2(w.balance_total + payout)
            w.balance_available = _round2(w.balance_available + payout)

            tx2 = Transaction(
                user_id=user_id,
                type="dice_payout",
                amount=float(payout),
                balance_after=float(w.balance_total),
                reference=None,
            )
            db.add(tx2)

            if float(payout) >= 5:
                _push_live_feed_event(
                    game="dice",
                    user_id=user_id,
                    amount_usd=float(amount),
                    payout=float(payout),
                    multiplier=float(multiplier),
                    source="real",
                )


        bet = DiceBet(
            user_id=user_id,
            amount_usd=float(amount),
            condition=condition,
            target=float(target),
            roll=float(roll),
            win=bool(win),
            multiplier=float(multiplier),
            payout=float(payout),
            server_seed_hash=s.server_seed_hash,
            client_seed=s.client_seed,
            nonce=int(s.nonce),
        )
        db.add(bet)
        db.flush()

        # Fill references after bet id exists
        tx1.reference = f"dice_bet:{bet.id}"
        if win:
            tx2.reference = f"dice_bet:{bet.id}"  # type: ignore[name-defined]


        if win and float(payout) >= 1:
            _push_live_feed_event(
                game="dice",
                user_id=user_id,
                amount_usd=float(amount),
                payout=float(payout),
                multiplier=float(multiplier),
                source="real",
                provider="coin2win",
            )

        response_payload = {
            "ok": True,
            "bet_id": bet.id,
            "user_id": user_id,
            "amount_usd": float(amount),
            "condition": condition,
            "target": float(target),
            "roll": float(roll),
            "win": bool(win),
            "multiplier": float(multiplier),
            "payout": float(payout),
            "server_seed_hash": s.server_seed_hash,
            "client_seed": s.client_seed,
            "nonce": int(s.nonce),
            "wallet": _serialize_wallet(w),
        }

        used_nonce = int(s.nonce)

        db.commit()

        # Auto-rotate AFTER the current bet is fully committed.
        # This keeps the just-finished bet tied to the old seed session,
        # and makes the NEXT bet start under a fresh server seed.
        if used_nonce >= DICE_AUTO_ROTATE_AFTER:
            try:
                fresh_seed_row = (
                    db.query(DiceSeed)
                    .filter(DiceSeed.user_id == user_id)
                    .with_for_update()
                    .one()
                )

                fresh_seed_row.prev_server_seed = fresh_seed_row.server_seed
                fresh_seed_row.prev_server_seed_hash = fresh_seed_row.server_seed_hash

                new_seed = secrets.token_hex(32)
                fresh_seed_row.server_seed = new_seed
                fresh_seed_row.server_seed_hash = _sha256_hex(new_seed)
                fresh_seed_row.nonce = 0

                db.commit()
            except Exception:
                db.rollback()

        return response_payload
    finally:
        db.close()

@app.get("/studio/dice/bets/{user_id}")
def dice_bets(
    user_id: str,
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
):
    db = SessionLocal()
    try:
        query = db.query(DiceBet).filter(DiceBet.user_id == user_id)

        if start_date:
            start_dt = datetime.fromisoformat(f"{start_date}T00:00:00")
            query = query.filter(DiceBet.created_at >= start_dt)

        if end_date:
            end_dt = datetime.fromisoformat(f"{end_date}T23:59:59.999999")
            query = query.filter(DiceBet.created_at <= end_dt)

        rows = (
            query.order_by(DiceBet.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "user_id": user_id,
            "count": len(rows),
            "bets": [
                {
                    "id": b.id,
                    "amount_usd": float(b.amount_usd),
                    "condition": b.condition,
                    "target": float(b.target),
                    "roll": float(b.roll),
                    "win": bool(b.win),
                    "multiplier": float(b.multiplier),
                    "payout": float(b.payout),
                    "server_seed_hash": b.server_seed_hash,
                    "client_seed": b.client_seed,
                    "nonce": int(b.nonce),
                    "created_at": str(b.created_at),
                }
                for b in rows
            ],
        }
    finally:
        db.close()


# ==========================
# STUDIO: DICE (Option A - Instant)
# ==========================
import secrets
import math
from sqlalchemy import Boolean

HOUSE_EDGE = float(os.getenv("DICE_HOUSE_EDGE", "1.0"))  # percent, e.g. 1.0 = 1%
DICE_AUTO_ROTATE_AFTER = 50


@app.get("/studio/dice/fair/{user_id}")
def dice_fair_state(user_id: str):
    db = SessionLocal()
    try:
        s = db.query(DiceSeed).filter(DiceSeed.user_id == user_id).one_or_none()
        if not s:
            raise HTTPException(status_code=404, detail="Dice seed not found for user")

        return {
            "user_id": user_id,
            "current_server_seed_hash": s.server_seed_hash,
            "current_client_seed": s.client_seed,
            "current_nonce": s.nonce,
            "revealed_prev_server_seed": s.prev_server_seed,
            "revealed_prev_server_seed_hash": s.prev_server_seed_hash,
        }
    finally:
        db.close()

@app.post("/studio/dice/verify")
async def dice_verify(request: Request):
    """
    Verify a roll from revealed server seed + client seed + nonce.
    Body:
    {
      "server_seed": "...",
      "client_seed": "...",
      "nonce": 1
    }
    """
    body = await request.json()

    server_seed = str(body.get("server_seed", "")).strip()
    client_seed = str(body.get("client_seed", "")).strip()
    nonce = int(body.get("nonce", 0))

    if not server_seed:
        raise HTTPException(status_code=400, detail="server_seed required")
    if not client_seed:
        raise HTTPException(status_code=400, detail="client_seed required")
    if nonce <= 0:
        raise HTTPException(status_code=400, detail="nonce must be >= 1")

    roll = _dice_roll(server_seed, client_seed, nonce)
    server_seed_hash = _sha256_hex(server_seed)

    return {
        "ok": True,
        "server_seed_hash": server_seed_hash,
        "client_seed": client_seed,
        "nonce": nonce,
        "roll": roll,
    }


# ==========================
# STUDIO: CRASH (Step 1 - Models + Seed State)
# ==========================
class CrashSeed(Base):
    __tablename__ = "crash_seeds"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), unique=True, nullable=False)

    server_seed = Column(Text, nullable=False)
    server_seed_hash = Column(String(64), nullable=False)

    client_seed = Column(Text, nullable=False, default="client_default")
    nonce = Column(Integer, nullable=False, default=0)

    prev_server_seed = Column(Text, nullable=True)
    prev_server_seed_hash = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class CrashRound(Base):
    __tablename__ = "crash_rounds"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)

    server_seed_hash = Column(String(64), nullable=False)
    client_seed = Column(Text, nullable=False)
    nonce = Column(Integer, nullable=False)

    crash_point = Column(Float, nullable=False)

    status = Column(String(32), nullable=False, default="created")  # created, running, crashed, completed

    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class CrashBet(Base):
    __tablename__ = "crash_bets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    round_id = Column(Integer, ForeignKey("crash_rounds.id"), nullable=False)

    amount_usd = Column(Float, nullable=False)
    auto_cashout = Column(Float, nullable=True)

    status = Column(String(32), nullable=False, default="active")  # active, cashed_out, lost
    cashout_multiplier = Column(Float, nullable=True)
    payout = Column(Float, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

def _get_or_create_crash_seed(db, user_id: str) -> CrashSeed:
    row = db.query(CrashSeed).filter(CrashSeed.user_id == user_id).one_or_none()
    if row:
        return row

    server_seed = secrets.token_hex(32)
    row = CrashSeed(
        user_id=user_id,
        server_seed=server_seed,
        server_seed_hash=_sha256_hex(server_seed),
        client_seed="client_default",
        nonce=0,
    )
    db.add(row)
    db.flush()
    return row

@app.get("/studio/crash/seed/{user_id}")
def crash_get_seed(user_id: str):
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        s = _get_or_create_crash_seed(db, user_id)
        db.commit()
        return {
            "user_id": user_id,
            "server_seed_hash": s.server_seed_hash,
            "client_seed": s.client_seed,
            "nonce_next": s.nonce + 1,
            "revealed_prev_server_seed": s.prev_server_seed,
            "revealed_prev_server_seed_hash": s.prev_server_seed_hash,
        }
    finally:
        db.close()

@app.post("/studio/crash/seed/{user_id}/client")
async def crash_set_client_seed(user_id: str, request: Request):
    body = await request.json()
    client_seed = str(body.get("client_seed", "")).strip()
    if not client_seed:
        raise HTTPException(status_code=400, detail="client_seed required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        s = _get_or_create_crash_seed(db, user_id)
        s.client_seed = client_seed
        db.commit()
        return {"ok": True, "user_id": user_id, "client_seed": s.client_seed}
    finally:
        db.close()

@app.post("/studio/crash/seed/{user_id}/rotate")
def crash_rotate_server_seed(user_id: str):
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        s = db.query(CrashSeed).filter(CrashSeed.user_id == user_id).with_for_update().one_or_none()
        if not s:
            s = _get_or_create_crash_seed(db, user_id)

        s.prev_server_seed = s.server_seed
        s.prev_server_seed_hash = s.server_seed_hash

        new_seed = secrets.token_hex(32)
        s.server_seed = new_seed
        s.server_seed_hash = _sha256_hex(new_seed)
        s.nonce = 0

        db.commit()
        return {
            "ok": True,
            "user_id": user_id,
            "new_server_seed_hash": s.server_seed_hash,
            "revealed_prev_server_seed": s.prev_server_seed,
            "revealed_prev_server_seed_hash": s.prev_server_seed_hash,
            "client_seed": s.client_seed,
            "nonce_reset_to": 0,
        }
    finally:
        db.close()

@app.get("/studio/crash/rounds/{user_id}")
def crash_rounds(user_id: str, limit: int = 50):
    db = SessionLocal()
    try:
        rows = (
            db.query(CrashRound)
            .filter(CrashRound.user_id == user_id)
            .order_by(CrashRound.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "user_id": user_id,
            "count": len(rows),
            "rounds": [
                {
                    "id": r.id,
                    "server_seed_hash": r.server_seed_hash,
                    "client_seed": r.client_seed,
                    "nonce": r.nonce,
                    "crash_point": float(r.crash_point),
                    "status": r.status,
                    "started_at": str(r.started_at) if r.started_at else None,
                    "ended_at": str(r.ended_at) if r.ended_at else None,
                    "created_at": str(r.created_at),
                }
                for r in rows
            ],
        }
    finally:
        db.close()



def _crash_point_from_seed(server_seed: str, client_seed: str, nonce: int) -> float:
    """
    Simple provably-fair crash formula for MVP.
    Produces a crash point >= 1.00
    """
    msg = f"{client_seed}:{nonce}"
    h = hmac.new(server_seed.encode(), msg.encode(), hashlib.sha256).hexdigest()

    n = int(h[:13], 16)
    r = n / float(2**52)

    # House edge built in lightly through formula shape
    # Keep crash point in a sane range for MVP
    if r >= 0.9999:
        return 100.0

    point = 0.99 / (1.0 - r)
    point = max(1.0, min(point, 100.0))
    return round(point, 2)


@app.post("/studio/crash/verify")
async def crash_verify(request: Request):
    """
    Verify a crash point from revealed server seed + client seed + nonce.
    Body:
    {
      "server_seed": "...",
      "client_seed": "...",
      "nonce": 1
    }
    """
    body = await request.json()

    server_seed = str(body.get("server_seed", "")).strip()
    client_seed = str(body.get("client_seed", "")).strip()
    nonce = int(body.get("nonce", 0))

    if not server_seed:
        raise HTTPException(status_code=400, detail="server_seed required")
    if not client_seed:
        raise HTTPException(status_code=400, detail="client_seed required")
    if nonce <= 0:
        raise HTTPException(status_code=400, detail="nonce must be >= 1")

    crash_point = _crash_point_from_seed(server_seed, client_seed, nonce)
    server_seed_hash = _sha256_hex(server_seed)

    return {
        "ok": True,
        "server_seed_hash": server_seed_hash,
        "client_seed": client_seed,
        "nonce": nonce,
        "crash_point": float(crash_point),
    }



def _crash_elapsed_seconds(started_at) -> float:
    from datetime import datetime, timezone

    if not started_at:
        return 0.0

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    return max(0.0, (now - started_at).total_seconds())


def _crash_live_multiplier_from_elapsed(elapsed_seconds: float) -> float:
    """
    Live crash pacing curve for MVP.
    ~1.38x at 2s, ~1.90x at 4s, ~2.61x at 6s, ~4.95x at 10s
    """
    value = math.exp(elapsed_seconds * 0.16)
    return round(max(1.0, value), 2)


def _crash_round_live_state(round_row) -> dict:
    round_status = str(round_row.status or "running").lower()

    if round_status == "completed":
        return {
            "round_id": round_row.id,
            "status": "completed",
            "current_multiplier": float(round_row.crash_point),
            "crash_point": float(round_row.crash_point),
            "crashed": False,
            "elapsed_seconds": _crash_elapsed_seconds(round_row.started_at),
        }

    if round_status == "crashed":
        return {
            "round_id": round_row.id,
            "status": "crashed",
            "current_multiplier": float(round_row.crash_point),
            "crash_point": float(round_row.crash_point),
            "crashed": True,
            "elapsed_seconds": _crash_elapsed_seconds(round_row.started_at),
        }

    elapsed_seconds = _crash_elapsed_seconds(round_row.started_at)
    live_multiplier = _crash_live_multiplier_from_elapsed(elapsed_seconds)
    crash_point = float(round_row.crash_point)
    crashed = live_multiplier >= crash_point

    return {
        "round_id": round_row.id,
        "status": "crashed" if crashed else "running",
        "current_multiplier": crash_point if crashed else live_multiplier,
        "crash_point": crash_point,
        "crashed": crashed,
        "elapsed_seconds": elapsed_seconds,
    }


def _crash_settle_round_if_needed(db, round_row):
    live = _crash_round_live_state(round_row)

    if str(round_row.status or "").lower() in ("completed", "crashed"):
        return live

    bet = (
        db.query(CrashBet)
        .filter(CrashBet.round_id == round_row.id)
        .order_by(CrashBet.id.desc())
        .one_or_none()
    )

    if not bet:
        return live

    # Auto cashout wins before crash
    if (
        bet.status == "active"
        and bet.auto_cashout is not None
        and float(bet.auto_cashout) <= float(round_row.crash_point)
        and live["current_multiplier"] >= float(bet.auto_cashout)
        and not live["crashed"]
    ):
        wallet = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()

        payout = _round2(float(bet.amount_usd) * float(bet.auto_cashout))

        wallet.balance_total = _round2(wallet.balance_total + payout)
        wallet.balance_available = _round2(wallet.balance_available + payout)

        bet.status = "cashed_out"
        bet.cashout_multiplier = float(bet.auto_cashout)
        bet.payout = payout

        if float(payout) >= 1:
            _push_live_feed_event(
                game="crash",
                user_id=bet.user_id,
                amount_usd=float(bet.amount_usd),
                payout=float(payout),
                multiplier=float(bet.auto_cashout),
                source="real",
                provider="coin2win",
            )


        round_row.status = "completed"
        round_row.ended_at = func.now()

        tx = Transaction(
            user_id=user_id,
            type="crash_payout",
            amount=float(payout),
            balance_after=float(wallet.balance_total),
            reference=f"crash_bet:{bet.id}",
        )
        db.add(tx)

        return _crash_round_live_state(round_row)

    # If crash point has been reached, unresolved active bet loses
    if bet.status == "active" and live["crashed"]:
        bet.status = "lost"
        bet.cashout_multiplier = None
        bet.payout = 0.0

        round_row.status = "crashed"
        round_row.ended_at = func.now()

        return _crash_round_live_state(round_row)

    return live


@app.get("/studio/crash/live/{round_id}")
def crash_live_round(round_id: int):
    db = SessionLocal()
    try:
        round_row = (
            db.query(CrashRound)
            .filter(CrashRound.id == round_id)
            .with_for_update()
            .one_or_none()
        )
        if not round_row:
            raise HTTPException(status_code=404, detail="Crash round not found")

        live = _crash_settle_round_if_needed(db, round_row)
        db.commit()

        bet = (
            db.query(CrashBet)
            .filter(CrashBet.round_id == round_id)
            .order_by(CrashBet.id.desc())
            .one_or_none()
        )

        return {
            "ok": True,
            "round_id": round_row.id,
            "user_id": round_row.user_id,
            "status": str(round_row.status),
            "current_multiplier": float(live["current_multiplier"]),
            "crash_point": float(round_row.crash_point),
            "crashed": bool(live["crashed"]),
            "elapsed_seconds": float(live["elapsed_seconds"]),
            "server_seed_hash": round_row.server_seed_hash,
            "client_seed": round_row.client_seed,
            "nonce": round_row.nonce,
            "started_at": str(round_row.started_at) if round_row.started_at else None,
            "ended_at": str(round_row.ended_at) if round_row.ended_at else None,
            "bet": {
                "id": bet.id,
                "user_id": bet.user_id,
                "amount_usd": float(bet.amount_usd),
                "auto_cashout": float(bet.auto_cashout) if bet.auto_cashout is not None else None,
                "status": str(bet.status),
                "cashout_multiplier": float(bet.cashout_multiplier) if bet.cashout_multiplier is not None else None,
                "payout": float(bet.payout),
                "created_at": str(bet.created_at),
            } if bet else None,
        }
    finally:
        db.close()


@app.post("/studio/crash/bet")
async def crash_bet(request: Request):
    """
    Body:
    {
      "user_id":"user_1",
      "amount_usd":1,
      "auto_cashout": 1.50   # optional
    }
    """
    body = await request.json()

    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))
    auto_cashout = body.get("auto_cashout", None)

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")

    if auto_cashout is not None:
        auto_cashout = float(auto_cashout)
        if auto_cashout <= 1.0:
            raise HTTPException(status_code=400, detail="auto_cashout must be > 1.0")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        _get_or_create_crash_seed(db, user_id)

        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()
        seed = db.query(CrashSeed).filter(CrashSeed.user_id == user_id).with_for_update().one()

        if float(wallet.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        # debit stake immediately
        wallet.balance_total = _round2(wallet.balance_total - amount)
        wallet.balance_available = _round2(wallet.balance_available - amount)

        tx1 = Transaction(
            user_id=user_id,
            type="crash_bet",
            amount=-float(amount),
            balance_after=float(wallet.balance_total),
            reference=None,
        )
        db.add(tx1)

        # advance seed nonce and create a live round
        seed.nonce += 1
        crash_point = _crash_point_from_seed(seed.server_seed, seed.client_seed, seed.nonce)

        round_row = CrashRound(
            user_id=user_id,
            server_seed_hash=seed.server_seed_hash,
            client_seed=seed.client_seed,
            nonce=seed.nonce,
            crash_point=crash_point,
            status="running",
            started_at=func.now(),
        )
        db.add(round_row)
        db.flush()

        bet = CrashBet(
            user_id=user_id,
            round_id=round_row.id,
            amount_usd=float(amount),
            auto_cashout=auto_cashout,
            status="active",
            cashout_multiplier=None,
            payout=0,
        )
        db.add(bet)
        db.flush()

        tx1.reference = f"crash_bet:{bet.id}"

        db.commit()

        return {
            "ok": True,
            "round_id": round_row.id,
            "bet_id": bet.id,
            "user_id": user_id,
            "amount_usd": amount,
            "auto_cashout": auto_cashout,
            "crash_point": None,  # hidden until round resolves
            "status": bet.status,
            "payout": float(bet.payout),
            "server_seed_hash": seed.server_seed_hash,
            "client_seed": seed.client_seed,
            "nonce": seed.nonce,
            "wallet": _serialize_wallet(wallet),
        }
    finally:
        db.close()

@app.post("/studio/crash/cashout/{bet_id}")
def crash_manual_cashout(bet_id: int):
    db = SessionLocal()
    try:
        bet = db.query(CrashBet).filter(CrashBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="Crash bet not found")

        round_row = db.query(CrashRound).filter(CrashRound.id == bet.round_id).with_for_update().one()
        wallet = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()

        live = _crash_settle_round_if_needed(db, round_row)

        if bet.status != "active":
            db.commit()
            return {
                "ok": True,
                "bet_id": bet.id,
                "status": bet.status,
                "cashout_multiplier": float(bet.cashout_multiplier) if bet.cashout_multiplier is not None else None,
                "crash_point": float(round_row.crash_point),
                "payout": float(bet.payout),
                "wallet": _serialize_wallet(wallet),
            }

        if live["crashed"]:
            bet.status = "lost"
            bet.cashout_multiplier = None
            bet.payout = 0.0

            round_row.status = "crashed"
            round_row.ended_at = func.now()

            db.commit()
            return {
                "ok": True,
                "bet_id": bet.id,
                "status": bet.status,
                "crash_point": float(round_row.crash_point),
                "payout": 0.0,
                "wallet": _serialize_wallet(wallet),
            }

        manual_cashout_multiplier = float(live["current_multiplier"])
        payout = _round2(float(bet.amount_usd) * manual_cashout_multiplier)

        wallet.balance_total = _round2(wallet.balance_total + payout)
        wallet.balance_available = _round2(wallet.balance_available + payout)

        bet.status = "cashed_out"
        bet.cashout_multiplier = manual_cashout_multiplier
        bet.payout = payout

        round_row.status = "completed"
        round_row.ended_at = func.now()

        tx = Transaction(
            user_id=bet.user_id,
            type="crash_payout",
            amount=float(payout),
            balance_after=float(wallet.balance_total),
            reference=f"crash_bet:{bet.id}",
        )
        db.add(tx)

        if float(payout) >= 1:
            _push_live_feed_event(
                game="crash",
                user_id=bet.user_id,
                amount_usd=float(bet.amount_usd),
                payout=float(payout),
                multiplier=float(manual_cashout_multiplier),
                source="real",
                provider="coin2win",
            )

        db.commit()

        return {
            "ok": True,
            "bet_id": bet.id,
            "status": bet.status,
            "cashout_multiplier": manual_cashout_multiplier,
            "crash_point": float(round_row.crash_point),
            "payout": float(payout),
            "wallet": _serialize_wallet(wallet),
        }
    finally:
        db.close()

@app.post("/studio/crash/resolve/{bet_id}")
def crash_resolve_unfinished(bet_id: int):
    """
    Finalize an active manual-cashout bet only if the live round has already crashed.
    """
    db = SessionLocal()
    try:
        bet = db.query(CrashBet).filter(CrashBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="Crash bet not found")

        round_row = db.query(CrashRound).filter(CrashRound.id == bet.round_id).with_for_update().one()

        live = _crash_settle_round_if_needed(db, round_row)

        if bet.status != "active":
            db.commit()
            return {
                "ok": True,
                "bet_id": bet.id,
                "status": bet.status,
                "payout": float(bet.payout),
                "crash_point": float(round_row.crash_point),
            }

        if not live["crashed"]:
            db.commit()
            return {
                "ok": True,
                "bet_id": bet.id,
                "status": "running",
                "current_multiplier": float(live["current_multiplier"]),
                "crash_point": None,
                "payout": float(bet.payout),
            }

        bet.status = "lost"
        bet.cashout_multiplier = None
        bet.payout = 0.0

        round_row.status = "crashed"
        round_row.ended_at = func.now()

        db.commit()

        return {
            "ok": True,
            "bet_id": bet.id,
            "status": bet.status,
            "crash_point": float(round_row.crash_point),
            "payout": 0.0,
        }
    finally:
        db.close()

@app.get("/studio/crash/bets/{user_id}")
def crash_bets(user_id: str, limit: int = 50):
    db = SessionLocal()
    try:
        rows = (
            db.query(CrashBet)
            .filter(CrashBet.user_id == user_id)
            .order_by(CrashBet.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "user_id": user_id,
            "count": len(rows),
            "bets": [
                {
                    "id": b.id,
                    "round_id": b.round_id,
                    "amount_usd": float(b.amount_usd),
                    "auto_cashout": float(b.auto_cashout) if b.auto_cashout is not None else None,
                    "status": b.status,
                    "cashout_multiplier": float(b.cashout_multiplier) if b.cashout_multiplier is not None else None,
                    "payout": float(b.payout),
                    "created_at": str(b.created_at),
                }
                for b in rows
            ],
        }
    finally:
        db.close()




# ==========================
# GLOBAL CRASH V2 RUNTIME
# ==========================

def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _ensure_aware(dt):
    from datetime import timezone
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _global_crash_live_multiplier_from_elapsed(elapsed_seconds: float) -> float:
    """
    Smooth crash pacing:
    ~1.38x at 2s, ~1.90x at 4s, ~2.61x at 6s, ~4.95x at 10s
    """
    value = math.exp(max(0.0, elapsed_seconds) * 0.16)
    return round(max(1.0, value), 2)


def _global_crash_round_state(round_row) -> dict:
    now = _utcnow()
    starts_at = _ensure_aware(round_row.starts_at)
    crashed_at = _ensure_aware(round_row.crashed_at)

    status = str(round_row.status or "betting").lower()

    if status == "betting":
        seconds_until_start = max(0.0, (starts_at - now).total_seconds()) if starts_at else 0.0
        return {
            "status": "betting",
            "current_multiplier": 1.0,
            "seconds_until_start": seconds_until_start,
            "elapsed_seconds": 0.0,
            "crashed": False,
        }

    if status == "crashed":
        return {
            "status": "crashed",
            "current_multiplier": float(round_row.crash_point),
            "seconds_until_start": 0.0,
            "elapsed_seconds": max(0.0, (_ensure_aware(crashed_at) - starts_at).total_seconds()) if crashed_at and starts_at else 0.0,
            "crashed": True,
        }

    elapsed_seconds = max(0.0, (now - starts_at).total_seconds()) if starts_at else 0.0
    live_multiplier = _global_crash_live_multiplier_from_elapsed(elapsed_seconds)
    crash_point = float(round_row.crash_point)
    crashed = live_multiplier >= crash_point

    return {
        "status": "crashed" if crashed else "running",
        "current_multiplier": crash_point if crashed else live_multiplier,
        "seconds_until_start": 0.0,
        "elapsed_seconds": elapsed_seconds,
        "crashed": crashed,
    }


def _get_or_create_global_crash_seed(db):
    return _get_or_create_crash_seed(db, GLOBAL_CRASH_ENGINE_USER)


def _global_crash_create_betting_round(db):
    from datetime import timedelta

    seed = _get_or_create_global_crash_seed(db)

    seed.nonce += 1
    crash_point = _crash_point_from_seed(seed.server_seed, seed.client_seed, seed.nonce)

    now = _utcnow()
    starts_at = now + timedelta(seconds=GLOBAL_CRASH_BETTING_SECONDS)

    round_row = GlobalCrashRound(
        server_seed_hash=seed.server_seed_hash,
        client_seed=seed.client_seed,
        nonce=seed.nonce,
        crash_point=crash_point,
        status="betting",
        betting_started_at=now,
        starts_at=starts_at,
    )
    db.add(round_row)
    db.flush()

    # Auto-rotate AFTER the current global round has captured the active seed/hash/nonce.
    # This makes the NEXT global round start under a fresh seed.
    if int(seed.nonce) >= GLOBAL_CRASH_AUTO_ROTATE_AFTER:
        seed.prev_server_seed = seed.server_seed
        seed.prev_server_seed_hash = seed.server_seed_hash

        new_seed = secrets.token_hex(32)
        seed.server_seed = new_seed
        seed.server_seed_hash = _sha256_hex(new_seed)
        seed.nonce = 0

    return round_row


def _global_crash_settle_auto_cashouts(db, round_row, current_multiplier: float):
    bets = (
        db.query(GlobalCrashBet)
        .filter(
            GlobalCrashBet.round_id == round_row.id,
            GlobalCrashBet.status == "active",
            GlobalCrashBet.auto_cashout.isnot(None),
        )
        .with_for_update()
        .all()
    )

    for bet in bets:
        target = float(bet.auto_cashout)
        if target <= float(current_multiplier) and target < float(round_row.crash_point):
            wallet = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()

            payout = _round2(float(bet.amount_usd) * target)

            wallet.balance_total = _round2(wallet.balance_total + payout)
            wallet.balance_available = _round2(wallet.balance_available + payout)

            bet.status = "cashed_out"
            bet.cashout_multiplier = target
            bet.payout = payout
            bet.settled_at = _utcnow()

            tx = Transaction(
                user_id=bet.user_id,
                type="crash_payout",
                amount=float(payout),
                balance_after=float(wallet.balance_total),
                reference=f"global_crash_bet:{bet.id}",
            )
            db.add(tx)


def _global_crash_settle_losses(db, round_row):
    bets = (
        db.query(GlobalCrashBet)
        .filter(
            GlobalCrashBet.round_id == round_row.id,
            GlobalCrashBet.status == "active",
        )
        .with_for_update()
        .all()
    )

    for bet in bets:
        bet.status = "lost"
        bet.cashout_multiplier = None
        bet.payout = 0.0
        bet.settled_at = _utcnow()


def _global_crash_engine_tick():
    db = SessionLocal()
    try:
        from datetime import timedelta

        round_row = db.query(GlobalCrashRound).order_by(GlobalCrashRound.id.desc()).with_for_update().first()

        if not round_row:
            _global_crash_create_betting_round(db)
            db.commit()
            return

        state = _global_crash_round_state(round_row)
        now = _utcnow()
        status = str(round_row.status or "betting").lower()

        if status == "betting":
            starts_at = _ensure_aware(round_row.starts_at)
            if starts_at and now >= starts_at:
                round_row.status = "running"
                db.commit()
            else:
                db.commit()
            return

        if status == "running":
            _global_crash_settle_auto_cashouts(db, round_row, float(state["current_multiplier"]))

            state = _global_crash_round_state(round_row)
            if state["crashed"]:
                round_row.status = "crashed"
                round_row.crashed_at = now
                round_row.ended_at = now
                round_row.cooldown_until = now + timedelta(seconds=GLOBAL_CRASH_COOLDOWN_SECONDS)
                _global_crash_settle_losses(db, round_row)

            db.commit()
            return

        if status == "crashed":
            cooldown_until = _ensure_aware(round_row.cooldown_until)
            if cooldown_until and now >= cooldown_until:
                _global_crash_create_betting_round(db)
            db.commit()
            return

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _global_crash_engine_loop():
    while True:
        try:
            _global_crash_engine_tick()
        except Exception as e:
            print("GLOBAL CRASH ENGINE ERROR:", e)
        time.sleep(GLOBAL_CRASH_TICK_SECONDS)


@app.on_event("startup")
def start_global_crash_engine():
    global GLOBAL_CRASH_ENGINE_STARTED

    if GLOBAL_CRASH_ENGINE_STARTED:
        return

    GLOBAL_CRASH_ENGINE_STARTED = True

    import threading
    t = threading.Thread(target=_global_crash_engine_loop, daemon=True)
    t.start()


@app.get("/studio/crash-global/fair")
def crash_global_fair():
    db = SessionLocal()
    try:
        seed = _get_or_create_global_crash_seed(db)
        db.commit()
        return {
            "server_seed_hash": seed.server_seed_hash,
            "client_seed": seed.client_seed,
            "nonce_next": seed.nonce + 1,
            "revealed_prev_server_seed": seed.prev_server_seed,
            "revealed_prev_server_seed_hash": seed.prev_server_seed_hash,
        }
    finally:
        db.close()


@app.get("/studio/crash-global/current")
def crash_global_current():
    db = SessionLocal()
    try:
        round_row = db.query(GlobalCrashRound).order_by(GlobalCrashRound.id.desc()).first()
        if not round_row:
            raise HTTPException(status_code=404, detail="No global crash round found")

        state = _global_crash_round_state(round_row)

        return {
            "ok": True,
            "round": {
                "id": round_row.id,
                "status": str(state["status"]),
                "current_multiplier": float(state["current_multiplier"]),
                "seconds_until_start": float(state["seconds_until_start"]),
                "elapsed_seconds": float(state["elapsed_seconds"]),
                "crashed": bool(state["crashed"]),
                "server_seed_hash": round_row.server_seed_hash,
                "client_seed": round_row.client_seed,
                "nonce": round_row.nonce,
                "crash_point": float(round_row.crash_point) if state["crashed"] else None,
                "betting_started_at": str(round_row.betting_started_at) if round_row.betting_started_at else None,
                "starts_at": str(round_row.starts_at) if round_row.starts_at else None,
                "crashed_at": str(round_row.crashed_at) if round_row.crashed_at else None,
                "cooldown_until": str(round_row.cooldown_until) if round_row.cooldown_until else None,
                "ended_at": str(round_row.ended_at) if round_row.ended_at else None,
            }
        }
    finally:
        db.close()


@app.post("/studio/crash-global/bet")
async def crash_global_bet(request: Request):
    body = await request.json()

    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))
    auto_cashout = body.get("auto_cashout", None)

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")

    if auto_cashout is not None:
        auto_cashout = float(auto_cashout)
        if auto_cashout <= 1.0:
            raise HTTPException(status_code=400, detail="auto_cashout must be > 1.0")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        round_row = db.query(GlobalCrashRound).order_by(GlobalCrashRound.id.desc()).with_for_update().first()
        if not round_row:
            raise HTTPException(status_code=400, detail="No active global crash round")
        if str(round_row.status or "").lower() != "betting":
            raise HTTPException(status_code=400, detail="Betting window is closed")

        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if float(wallet.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        wallet.balance_total = _round2(wallet.balance_total - amount)
        wallet.balance_available = _round2(wallet.balance_available - amount)

        tx1 = Transaction(
            user_id=user_id,
            type="crash_bet",
            amount=-float(amount),
            balance_after=float(wallet.balance_total),
            reference=None,
        )
        db.add(tx1)

        bet = GlobalCrashBet(
            user_id=user_id,
            round_id=round_row.id,
            amount_usd=float(amount),
            auto_cashout=auto_cashout,
            status="active",
            cashout_multiplier=None,
            payout=0.0,
        )
        db.add(bet)
        db.flush()

        tx1.reference = f"global_crash_bet:{bet.id}"

        db.commit()

        return {
            "ok": True,
            "bet_id": bet.id,
            "round_id": round_row.id,
            "user_id": user_id,
            "amount_usd": float(amount),
            "auto_cashout": auto_cashout,
            "status": str(bet.status),
            "payout": float(bet.payout),
            "wallet": _serialize_wallet(wallet),
        }
    finally:
        db.close()


@app.post("/studio/crash-global/cashout/{bet_id}")
def crash_global_cashout(bet_id: int):
    db = SessionLocal()
    try:
        bet = db.query(GlobalCrashBet).filter(GlobalCrashBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="Global crash bet not found")

        round_row = db.query(GlobalCrashRound).filter(GlobalCrashRound.id == bet.round_id).with_for_update().one()
        wallet = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()

        if bet.status != "active":
            return {
                "ok": True,
                "bet_id": bet.id,
                "round_id": round_row.id,
                "status": str(bet.status),
                "cashout_multiplier": float(bet.cashout_multiplier) if bet.cashout_multiplier is not None else None,
                "payout": float(bet.payout),
                "wallet": _serialize_wallet(wallet),
            }

        state = _global_crash_round_state(round_row)

        if str(round_row.status or "").lower() != "running":
            raise HTTPException(status_code=400, detail="Round is not running")

        if state["crashed"]:
            bet.status = "lost"
            bet.cashout_multiplier = None
            bet.payout = 0.0
            bet.settled_at = _utcnow()
            db.commit()

            return {
                "ok": True,
                "bet_id": bet.id,
                "round_id": round_row.id,
                "status": "lost",
                "payout": 0.0,
                "wallet": _serialize_wallet(wallet),
            }

        manual_cashout_multiplier = float(state["current_multiplier"])
        payout = _round2(float(bet.amount_usd) * manual_cashout_multiplier)

        wallet.balance_total = _round2(wallet.balance_total + payout)
        wallet.balance_available = _round2(wallet.balance_available + payout)

        bet.status = "cashed_out"
        bet.cashout_multiplier = manual_cashout_multiplier
        bet.payout = payout
        bet.settled_at = _utcnow()

        tx = Transaction(
            user_id=bet.user_id,
            type="crash_payout",
            amount=float(payout),
            balance_after=float(wallet.balance_total),
            reference=f"global_crash_bet:{bet.id}",
        )
        db.add(tx)

        if float(payout) >= 1:
            _push_live_feed_event(
                game="crash",
                user_id=bet.user_id,
                amount_usd=float(bet.amount_usd),
                payout=float(payout),
                multiplier=float(manual_cashout_multiplier),
                source="real",
                provider="coin2win",
            )


        db.commit()

        return {
            "ok": True,
            "bet_id": bet.id,
            "round_id": round_row.id,
            "status": "cashed_out",
            "cashout_multiplier": manual_cashout_multiplier,
            "payout": float(payout),
            "wallet": _serialize_wallet(wallet),
        }
    finally:
        db.close()


@app.get("/studio/crash-global/history")
def crash_global_history(limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(GlobalCrashRound)
            .order_by(GlobalCrashRound.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )

        return {
            "count": len(rows),
            "rounds": [
                {
                    "id": r.id,
                    "status": r.status,
                    "crash_point": float(r.crash_point),
                    "server_seed_hash": r.server_seed_hash,
                    "client_seed": r.client_seed,
                    "nonce": r.nonce,
                    "betting_started_at": str(r.betting_started_at) if r.betting_started_at else None,
                    "starts_at": str(r.starts_at) if r.starts_at else None,
                    "crashed_at": str(r.crashed_at) if r.crashed_at else None,
                    "cooldown_until": str(r.cooldown_until) if r.cooldown_until else None,
                    "ended_at": str(r.ended_at) if r.ended_at else None,
                    "created_at": str(r.created_at),
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@app.get("/studio/crash-global/my-bets/{user_id}")
def crash_global_my_bets(
    user_id: str,
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
):
    db = SessionLocal()
    try:
        query = db.query(GlobalCrashBet).filter(GlobalCrashBet.user_id == user_id)

        if start_date:
            start_dt = datetime.fromisoformat(f"{start_date}T00:00:00")
            query = query.filter(GlobalCrashBet.created_at >= start_dt)

        if end_date:
            end_dt = datetime.fromisoformat(f"{end_date}T23:59:59.999999")
            query = query.filter(GlobalCrashBet.created_at <= end_dt)

        rows = (
            query.order_by(GlobalCrashBet.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )

        round_ids = [int(b.round_id) for b in rows]
        rounds_by_id = {}

        if round_ids:
            round_rows = (
                db.query(GlobalCrashRound)
                .filter(GlobalCrashRound.id.in_(round_ids))
                .all()
            )
            rounds_by_id = {int(r.id): r for r in round_rows}

        return {
            "user_id": user_id,
            "count": len(rows),
            "bets": [
                {
                    "id": b.id,
                    "round_id": b.round_id,
                    "amount_usd": float(b.amount_usd),
                    "auto_cashout": float(b.auto_cashout) if b.auto_cashout is not None else None,
                    "status": str(b.status),
                    "cashout_multiplier": float(b.cashout_multiplier) if b.cashout_multiplier is not None else None,
                    "payout": float(b.payout),
                    "created_at": str(b.created_at),
                    "settled_at": str(b.settled_at) if b.settled_at else None,

                    "server_seed_hash": (
                        rounds_by_id[int(b.round_id)].server_seed_hash
                        if int(b.round_id) in rounds_by_id
                        else None
                    ),
                    "client_seed": (
                        rounds_by_id[int(b.round_id)].client_seed
                        if int(b.round_id) in rounds_by_id
                        else None
                    ),
                    "nonce": (
                        int(rounds_by_id[int(b.round_id)].nonce)
                        if int(b.round_id) in rounds_by_id
                        else None
                    ),
                    "crash_point": (
                        float(rounds_by_id[int(b.round_id)].crash_point)
                        if int(b.round_id) in rounds_by_id
                        else None
                    ),
                    "round_status": (
                        str(rounds_by_id[int(b.round_id)].status)
                        if int(b.round_id) in rounds_by_id
                        else None
                    ),
                    "betting_started_at": (
                        str(rounds_by_id[int(b.round_id)].betting_started_at)
                        if int(b.round_id) in rounds_by_id and rounds_by_id[int(b.round_id)].betting_started_at
                        else None
                    ),
                    "starts_at": (
                        str(rounds_by_id[int(b.round_id)].starts_at)
                        if int(b.round_id) in rounds_by_id and rounds_by_id[int(b.round_id)].starts_at
                        else None
                    ),
                    "crashed_at": (
                        str(rounds_by_id[int(b.round_id)].crashed_at)
                        if int(b.round_id) in rounds_by_id and rounds_by_id[int(b.round_id)].crashed_at
                        else None
                    ),
                    "ended_at": (
                        str(rounds_by_id[int(b.round_id)].ended_at)
                        if int(b.round_id) in rounds_by_id and rounds_by_id[int(b.round_id)].ended_at
                        else None
                    ),
                }
                for b in rows
            ],
        }
    finally:
        db.close()


from app.auth_routes import auth_router, init_auth_tables

init_auth_tables()
app.include_router(auth_router)

# --------------------------
# Public Activity Feed (Wins Only MVP)
# --------------------------

def _mask_public_user(user_id: str) -> str:
    raw = str(user_id or "player")
    if len(raw) <= 4:
        return raw[0] + "***" if raw else "p***"
    return raw[:1] + "***" + raw[-3:]


@app.get("/activity/wins")
def public_activity_wins(limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .filter(Transaction.type.in_(["dice_payout", "crash_payout"]))
            .order_by(Transaction.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )

        items = []
        for tx in rows:
            tx_type = str(tx.type or "").lower()

            if tx_type == "dice_payout":
                game = "Dice"
                event_type = "win"
            elif tx_type == "crash_payout":
                game = "Crash"
                event_type = "cashout"
            else:
                continue

            items.append({
                "id": tx.id,
                "user": _mask_public_user(tx.user_id),
                "game": game,
                "event_type": event_type,
                "amount": float(tx.amount) if tx.amount is not None else 0.0,
                "reference": tx.reference,
                "created_at": str(tx.created_at) if tx.created_at else None,
            })

        return {
            "count": len(items),
            "items": items,
        }
    finally:
        db.close()

# =========================
# KYC SYSTEM
# =========================

from fastapi import UploadFile, File, Form
from typing import Optional

@app.get("/kyc/me")
def kyc_me(user_id: str):
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        user = db.query(User).filter(User.id == user_id).one()

        return {
            "user_id": user.id,
            "kyc_status": getattr(user, "kyc_status", "unverified"),
            "kyc_verified_at": user.kyc_verified_at.isoformat() if getattr(user, "kyc_verified_at", None) else None,
            "kyc_rejected_reason": getattr(user, "kyc_rejected_reason", None),
        }
    finally:
        db.close()


@app.post("/kyc/upload")
async def upload_kyc(
    user_id: str = Form(...),
    document_front: UploadFile = File(...),
    document_back: UploadFile = File(...),
    selfie: Optional[UploadFile] = File(default=None),
):
    import os

    upload_dir = "/var/www/coin2win/uploads/kyc"
    os.makedirs(upload_dir, exist_ok=True)

    front_path = f"{upload_dir}/{user_id}_front_{document_front.filename}"
    back_path = f"{upload_dir}/{user_id}_back_{document_back.filename}"
    selfie_path = None

    with open(front_path, "wb") as f:
        f.write(await document_front.read())

    with open(back_path, "wb") as f:
        f.write(await document_back.read())

    if selfie is not None and getattr(selfie, "filename", None):
        selfie_path = f"{upload_dir}/{user_id}_selfie_{selfie.filename}"
        with open(selfie_path, "wb") as f:
            f.write(await selfie.read())

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        user = db.query(User).filter(User.id == user_id).one()
        user.kyc_status = "pending"
        user.kyc_rejected_reason = None
        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "message": "Documents uploaded",
        "user_id": user_id,
        "kyc_status": "pending",
        "files": {
            "front": front_path,
            "back": back_path,
            "selfie": selfie_path,
        }
    }


@app.post("/kyc/verify")
async def verify_kyc(user_id: str):
    # ADMIN ACTION (for now manual test)
    return {
        "ok": True,
        "user_id": user_id,
        "kyc_status": "verified"
    }


# =========================
# KYC ADMIN ENDPOINTS
# =========================

KYC_STORE = {}  # temporary in-memory store (replace with DB later)


@app.post("/kyc/submit")
async def submit_kyc(user_id: str):
    # create/update submission record
    KYC_STORE[user_id] = {
        "user_id": user_id,
        "kyc_status": "pending",
        "submitted_at": datetime.utcnow().isoformat()
    }
    return {"ok": True, "kyc_status": "pending"}


@app.get("/admin/kyc/submissions")
async def get_kyc_submissions():
    return {"submissions": list(KYC_STORE.values())}


@app.post("/admin/kyc/submissions/{user_id}/approve")
async def approve_kyc(user_id: str):
    if user_id in KYC_STORE:
        KYC_STORE[user_id]["kyc_status"] = "verified"
    return {"ok": True, "user_id": user_id, "status": "verified"}


@app.post("/admin/kyc/submissions/{user_id}/reject")
async def reject_kyc(user_id: str):
    if user_id in KYC_STORE:
        KYC_STORE[user_id]["kyc_status"] = "rejected"
    return {"ok": True, "user_id": user_id, "status": "rejected"}


# =========================
# REAL KYC ADMIN ENDPOINTS
# =========================

@app.get("/admin/kyc/users")
def admin_kyc_users(limit: int = 50, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    conn = engine.connect()
    try:
        rows = conn.execute(text("""
            SELECT id, role, kyc_status, kyc_verified_at, kyc_rejected_reason, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()

        return {
            "count": len(rows),
            "users": [
                {
                    "id": r.id,
                    "role": r.role,
                    "kyc_status": r.kyc_status or "unverified",
                    "kyc_verified_at": r.kyc_verified_at.isoformat() if r.kyc_verified_at else None,
                    "kyc_rejected_reason": r.kyc_rejected_reason,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


@app.post("/admin/kyc/users/{user_id}/approve")
def admin_kyc_user_approve(user_id: str):
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.kyc_status = "verified"
        user.kyc_verified_at = func.now()
        user.kyc_rejected_reason = None

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "kyc_status": "verified",
        }
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@app.post("/admin/kyc/users/{user_id}/reject")
async def admin_kyc_user_reject(user_id: str, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    body = await request.json()
    reason = str(body.get("reason", "")).strip() or "KYC rejected by admin"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.kyc_status = "rejected"
        user.kyc_verified_at = None
        user.kyc_rejected_reason = reason

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "kyc_status": "rejected",
            "reason": reason,
        }
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


# =========================
# KYC ADMIN FILE ENDPOINTS
# =========================

from pathlib import Path as _Path
from fastapi.responses import FileResponse

KYC_UPLOAD_DIR = _Path("/var/www/coin2win/uploads/kyc")

@app.get("/admin/kyc/files/{user_id}")
def admin_kyc_files(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    if not KYC_UPLOAD_DIR.exists():
        return {"user_id": user_id, "files": []}

    files = []
    for p in sorted(KYC_UPLOAD_DIR.iterdir()):
        if p.is_file() and p.name.startswith(f"{user_id}_"):
            label = "document"
            low = p.name.lower()
            if "front" in low:
                label = "front"
            elif "back" in low:
                label = "back"
            elif "selfie" in low:
                label = "selfie"

            files.append({
                "name": p.name,
                "label": label,
                "url": f"/api/admin/kyc/file/{user_id}/{p.name}",
            })

    return {"user_id": user_id, "files": files}


@app.get("/admin/kyc/file/{user_id}/{filename}")
def admin_kyc_file(user_id: str, filename: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    filename = str(filename).strip()

    if not user_id or not filename:
        raise HTTPException(status_code=400, detail="Invalid file request")

    target = KYC_UPLOAD_DIR / filename

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    if not target.name.startswith(f"{user_id}_"):
        raise HTTPException(status_code=403, detail="Forbidden")

    return FileResponse(str(target), filename=target.name)


# =========================
# HIERARCHY BACKEND
# =========================

from sqlalchemy import text as sa_text

HIERARCHY_ROLES = [
    "super_admin",
    "admin",
    "master_agent",
    "agent",
    "sub_agent",
    "player",
]

CHILD_ROLE_RULES = {
    "super_admin": {"admin", "master_agent", "agent", "sub_agent", "player"},
    "admin": {"master_agent", "agent", "sub_agent", "player"},
    "master_agent": {"agent", "sub_agent", "player"},
    "agent": {"sub_agent", "player"},
    "sub_agent": {"player"},
    "player": set(),
}


def _row_to_dict(row):
    if not row:
        return None
    return dict(row._mapping)


def _get_user_hierarchy_row(db, user_id: str):
    row = db.execute(
        sa_text("""
            SELECT
                id,
                role,
                parent_id,
                created_by,
                permissions_json,
                agent_code,
                is_active,
                created_at
            FROM users
            WHERE id = :user_id
            LIMIT 1
        """),
        {"user_id": user_id},
    ).fetchone()
    return _row_to_dict(row)


def _can_create_child_role(parent_role: str, child_role: str) -> bool:
    parent_role = str(parent_role or "").strip().lower()
    child_role = str(child_role or "").strip().lower()
    return child_role in CHILD_ROLE_RULES.get(parent_role, set())


def _get_subtree_rows(
    db,
    root_user_id: str,
    include_inactive: bool = False,
    inactive_only: bool = False,
):
    rows = db.execute(
        sa_text("""
            WITH RECURSIVE user_tree AS (
                SELECT
                    id,
                    role,
                    parent_id,
                    created_by,
                    permissions_json,
                    agent_code,
                    is_active,
                    created_at,
                    0 AS depth
                FROM users
                WHERE id = :root_user_id

                UNION ALL

                SELECT
                    u.id,
                    u.role,
                    u.parent_id,
                    u.created_by,
                    u.permissions_json,
                    u.agent_code,
                    u.is_active,
                    u.created_at,
                    ut.depth + 1 AS depth
                FROM users u
                INNER JOIN user_tree ut ON u.parent_id = ut.id
            )
            SELECT *
            FROM user_tree
            WHERE
                (:include_inactive = TRUE)
                OR (:inactive_only = TRUE AND is_active = FALSE)
                OR (:include_inactive = FALSE AND :inactive_only = FALSE AND is_active = TRUE)
            ORDER BY depth ASC, created_at ASC NULLS LAST, id ASC
        """),
        {
            "root_user_id": root_user_id,
            "include_inactive": include_inactive,
            "inactive_only": inactive_only,
        },
    ).fetchall()

    return [dict(r._mapping) for r in rows]


@app.get("/admin/hierarchy/user/{user_id}")
def admin_hierarchy_user(
    user_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        row = _get_user_hierarchy_row(db, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {"ok": True, "user": row}
    finally:
        db.close()


@app.get("/admin/hierarchy/tree/{user_id}")
def admin_hierarchy_tree(
    user_id: str,
    include_inactive: bool = False,
    inactive_only: bool = False,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        root = _get_user_hierarchy_row(db, user_id)
        if not root:
            raise HTTPException(status_code=404, detail="User not found")

        tree = _get_subtree_rows(
            db,
            user_id,
            include_inactive=include_inactive,
            inactive_only=inactive_only,
        )
        return {
            "ok": True,
            "root_user_id": user_id,
            "include_inactive": include_inactive,
            "inactive_only": inactive_only,
            "count": len(tree),
            "tree": tree,
        }
    finally:
        db.close()


@app.post("/admin/hierarchy/create")
async def admin_hierarchy_create(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    body = await request.json()

    user_id = str(body.get("user_id", "")).strip()
    parent_id = str(body.get("parent_id", "")).strip()
    role = str(body.get("role", "player")).strip().lower()
    created_by = str(body.get("created_by", "")).strip() or "admin"
    permissions_json = body.get("permissions_json")
    agent_code = str(body.get("agent_code", "")).strip() or None
    is_active = bool(body.get("is_active", True))

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not parent_id:
        raise HTTPException(status_code=400, detail="parent_id required")
    if role not in HIERARCHY_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    db = SessionLocal()
    try:
        parent = _get_user_hierarchy_row(db, parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent user not found")

        if not _can_create_child_role(parent["role"], role):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{parent['role']}' cannot create child role '{role}'",
            )

        existing = _get_user_hierarchy_row(db, user_id)
        if existing:
            raise HTTPException(status_code=400, detail="user_id already exists")

        db.execute(
            sa_text("""
                INSERT INTO users (
                    id,
                    role,
                    parent_id,
                    created_by,
                    permissions_json,
                    agent_code,
                    is_active
                )
                VALUES (
                    :id,
                    :role,
                    :parent_id,
                    :created_by,
                    :permissions_json,
                    :agent_code,
                    :is_active
                )
            """),
            {
                "id": user_id,
                "role": role,
                "parent_id": parent_id,
                "created_by": created_by,
                "permissions_json": permissions_json,
                "agent_code": agent_code,
                "is_active": is_active,
            },
        )
        db.commit()

        created = _get_user_hierarchy_row(db, user_id)

        return {
            "ok": True,
            "user": created,
        }
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


# =========================
# HIERARCHY ACCESS LOGIC
# =========================

def _can_view_admin_hierarchy(role: str) -> bool:
    return str(role or "").strip().lower() in {
        "super_admin",
        "admin",
        "master_agent",
        "agent",
        "sub_agent",
    }


def _is_descendant_or_self(db, ancestor_user_id: str, target_user_id: str) -> bool:
    rows = db.execute(
        sa_text("""
            WITH RECURSIVE user_tree AS (
                SELECT id, parent_id
                FROM users
                WHERE id = :ancestor_user_id

                UNION ALL

                SELECT u.id, u.parent_id
                FROM users u
                INNER JOIN user_tree ut ON u.parent_id = ut.id
            )
            SELECT 1
            FROM user_tree
            WHERE id = :target_user_id
            LIMIT 1
        """),
        {
            "ancestor_user_id": ancestor_user_id,
            "target_user_id": target_user_id,
        },
    ).fetchone()

    return rows is not None


def _enforce_hierarchy_scope(db, viewer_user_id: str, target_user_id: str):
    viewer = _get_user_hierarchy_row(db, viewer_user_id)
    if not viewer:
        raise HTTPException(status_code=404, detail="Viewer not found")

    viewer_role = str(viewer.get("role") or "").strip().lower()

    if not _can_view_admin_hierarchy(viewer_role):
        raise HTTPException(status_code=403, detail="Hierarchy access not allowed for this role")

    if viewer_role == "super_admin":
        return viewer

    if not _is_descendant_or_self(db, viewer_user_id, target_user_id):
        raise HTTPException(status_code=403, detail="Target user is outside your hierarchy scope")

    return viewer


@app.get("/admin/hierarchy/my-tree/{viewer_id}")
def admin_hierarchy_my_tree(
    viewer_id: str,
    include_inactive: bool = False,
    inactive_only: bool = False,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        viewer = _get_user_hierarchy_row(db, viewer_id)
        if not viewer:
            raise HTTPException(status_code=404, detail="Viewer not found")

        viewer_role = str(viewer.get("role") or "").strip().lower()
        if not _can_view_admin_hierarchy(viewer_role):
            raise HTTPException(status_code=403, detail="Hierarchy access not allowed for this role")

        tree = _get_subtree_rows(
            db,
            viewer_id,
            include_inactive=include_inactive,
            inactive_only=inactive_only,
        )
        return {
            "ok": True,
            "viewer_id": viewer_id,
            "viewer_role": viewer_role,
            "include_inactive": include_inactive,
            "inactive_only": inactive_only,
            "count": len(tree),
            "tree": tree,
        }
    finally:
        db.close()


@app.get("/admin/hierarchy/scoped-tree/{viewer_id}/{target_user_id}")
def admin_hierarchy_scoped_tree(
    viewer_id: str,
    target_user_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        viewer = _enforce_hierarchy_scope(db, viewer_id, target_user_id)
        tree = _get_subtree_rows(db, target_user_id)

        return {
            "ok": True,
            "viewer_id": viewer_id,
            "viewer_role": viewer.get("role"),
            "target_user_id": target_user_id,
            "count": len(tree),
            "tree": tree,
        }
    finally:
        db.close()


# =========================
# AGENT CREATION SYSTEM (SAFE VERSION)
# =========================

from fastapi import Body

ROLE_CREATE_MAP = {
    "super_admin": ["admin"],
    "admin": ["master_agent"],
    "master_agent": ["agent"],
    "agent": ["sub_agent"],
    "sub_agent": ["player"],
}

@app.post("/admin/agents/create")
async def create_agent(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    payload: dict = Body(...)
):
    _require_admin(x_admin_key)

    creator_id = payload.get("creator_id")
    new_user_id = payload.get("user_id")
    role = payload.get("role")

    if not creator_id or not new_user_id or not role:
        raise HTTPException(status_code=400, detail="Missing fields")

    conn = engine.connect()

    try:
        creator = conn.execute(
            text("SELECT id, role FROM users WHERE id = :id"),
            {"id": creator_id}
        ).fetchone()

        if not creator:
            raise HTTPException(status_code=404, detail="Creator not found")

        allowed_roles = ROLE_CREATE_MAP.get(creator.role, [])
        if role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Not allowed to create this role")

        existing = conn.execute(
            text("SELECT id FROM users WHERE id = :id"),
            {"id": new_user_id}
        ).fetchone()

        if existing:
            raise HTTPException(status_code=400, detail="User already exists")

        conn.execute(text("""
            INSERT INTO users (
                id, role, parent_id, created_by,
                is_active, created_at
            )
            VALUES (
                :id, :role, :parent_id, :created_by,
                TRUE, NOW()
            )
        """), {
            "id": new_user_id,
            "role": role,
            "parent_id": creator_id,
            "created_by": creator_id
        })

        conn.commit()

        return {
            "ok": True,
            "created_user": new_user_id,
            "role": role,
            "parent_id": creator_id
        }

    finally:
        conn.close()


# =========================
# PERMISSIONS SYSTEM
# =========================

ROLE_PERMISSIONS = {
    "super_admin": {
        "can_view_reports": True,
        "can_modify_odds": True,
        "can_approve_withdrawals": True,
        "can_view_downline": True,
        "can_create_users": True,
    },
    "admin": {
        "can_view_reports": True,
        "can_modify_odds": True,
        "can_approve_withdrawals": True,
        "can_view_downline": True,
        "can_create_users": True,
    },
    "master_agent": {
        "can_view_reports": True,
        "can_modify_odds": False,
        "can_approve_withdrawals": False,
        "can_view_downline": True,
        "can_create_users": True,
    },
    "agent": {
        "can_view_reports": True,
        "can_modify_odds": False,
        "can_approve_withdrawals": False,
        "can_view_downline": True,
        "can_create_users": True,
    },
    "sub_agent": {
        "can_view_reports": True,
        "can_modify_odds": False,
        "can_approve_withdrawals": False,
        "can_view_downline": True,
        "can_create_users": True,
    },
    "player": {
        "can_view_reports": False,
        "can_modify_odds": False,
        "can_approve_withdrawals": False,
        "can_view_downline": False,
        "can_create_users": False,
    }
}


def _get_user_permissions(user):
    if user.permissions_json:
        return user.permissions_json
    return ROLE_PERMISSIONS.get(user.role, {})


@app.get("/admin/permissions/{user_id}")
def get_user_permissions(
    user_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        user = conn.execute(
            text("SELECT id, role, permissions_json FROM users WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "ok": True,
            "user_id": user_id,
            "role": user.role,
            "permissions": _get_user_permissions(user)
        }

    finally:
        conn.close()


@app.post("/admin/permissions/{user_id}")
def set_user_permissions(
    user_id: str,
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        conn.execute(
            text("""
                UPDATE users
                SET permissions_json = :permissions
                WHERE id = :id
            """),
            {
                "id": user_id,
                "permissions": json.dumps(payload)
            }
        )

        conn.commit()

        return {"ok": True, "updated_user": user_id}

    finally:
        conn.close()


# =========================
# GGR SYSTEM (BASIC)
# =========================

@app.get("/admin/ggr/{agent_id}")
def get_agent_ggr(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        # get all players under agent
        players = conn.execute(text("""
            SELECT id FROM users WHERE parent_id = :agent_id
        """), {"agent_id": agent_id}).fetchall()

        player_ids = [p.id for p in players]

        if not player_ids:
            return {"ok": True, "ggr": 0, "players": 0}

        # sample logic (replace later with real bets table)
        total_losses = 0

        # placeholder: simulate or extend later
        for _ in player_ids:
            total_losses += 100  # fake data for now

        agent = conn.execute(text("""
            SELECT ggr_share FROM users WHERE id = :id
        """), {"id": agent_id}).fetchone()

        share = agent.ggr_share if agent and agent.ggr_share else 0

        earnings = total_losses * share

        return {
            "ok": True,
            "agent_id": agent_id,
            "players": len(player_ids),
            "total_losses": total_losses,
            "ggr_share": share,
            "earnings": earnings
        }

    finally:
        conn.close()


# =========================
# REAL GGR SYSTEM (CRASH + DICE)
# =========================

@app.get("/admin/ggr-real/{agent_id}")
def get_agent_ggr_real(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        # get all direct players under agent
        players = conn.execute(text("""
            SELECT id FROM users WHERE parent_id = :agent_id
        """), {"agent_id": agent_id}).fetchall()

        player_ids = [p.id for p in players]

        if not player_ids:
            return {"ok": True, "ggr": 0, "players": 0}

        # 🔥 Dice GGR
        dice = conn.execute(text(f"""
            SELECT 
                COALESCE(SUM(amount_usd),0) as wager,
                COALESCE(SUM(payout),0) as payout
            FROM dice_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": player_ids}).fetchone()

        # 🔥 Crash GGR
        crash = conn.execute(text(f"""
            SELECT 
                COALESCE(SUM(amount_usd),0) as wager,
                COALESCE(SUM(payout),0) as payout
            FROM crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": player_ids}).fetchone()

        total_wager = (dice.wager or 0) + (crash.wager or 0)
        total_payout = (dice.payout or 0) + (crash.payout or 0)

        ggr = total_wager - total_payout

        agent = conn.execute(text("""
            SELECT ggr_share FROM users WHERE id = :id
        """), {"id": agent_id}).fetchone()

        share = agent.ggr_share if agent and agent.ggr_share else 0
        earnings = ggr * share

        return {
            "ok": True,
            "agent_id": agent_id,
            "players": len(player_ids),
            "total_wager": total_wager,
            "total_payout": total_payout,
            "ggr": ggr,
            "ggr_share": share,
            "earnings": earnings
        }

    finally:
        conn.close()


# =========================
# FULL HIERARCHY GGR (RECURSIVE)
# =========================

def _get_all_downline_ids(conn, root_id):
    result = []
    stack = [root_id]

    while stack:
        current = stack.pop()
        children = conn.execute(text("""
            SELECT id FROM users WHERE parent_id = :id
        """), {"id": current}).fetchall()

        for c in children:
            result.append(c.id)
            stack.append(c.id)

    return result


@app.get("/admin/ggr-full/{agent_id}")
def get_agent_ggr_full(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        downline_ids = _get_all_downline_ids(conn, agent_id)

        if not downline_ids:
            return {"ok": True, "ggr": 0, "players": 0}

        # Dice
        dice = conn.execute(text("""
            SELECT 
                COALESCE(SUM(amount_usd),0) as wager,
                COALESCE(SUM(payout),0) as payout
            FROM dice_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        # Crash
        crash = conn.execute(text("""
            SELECT 
                COALESCE(SUM(amount_usd),0) as wager,
                COALESCE(SUM(payout),0) as payout
            FROM crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        total_wager = (dice.wager or 0) + (crash.wager or 0)
        total_payout = (dice.payout or 0) + (crash.payout or 0)

        ggr = total_wager - total_payout

        agent = conn.execute(text("""
            SELECT ggr_share FROM users WHERE id = :id
        """), {"id": agent_id}).fetchone()

        share = agent.ggr_share if agent and agent.ggr_share else 0
        earnings = ggr * share

        return {
            "ok": True,
            "agent_id": agent_id,
            "downline_count": len(downline_ids),
            "total_wager": total_wager,
            "total_payout": total_payout,
            "ggr": ggr,
            "ggr_share": share,
            "earnings": earnings
        }

    finally:
        conn.close()


# =========================
# AGENT LEDGER SYSTEM
# =========================

@app.post("/admin/ledger/credit")
def credit_agent(
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    agent_id = payload.get("agent_id")
    amount = payload.get("amount")

    if not agent_id or amount is None:
        raise HTTPException(status_code=400, detail="Missing fields")

    conn = engine.connect()

    try:
        conn.execute(text("""
            INSERT INTO agent_ledger (agent_id, amount, type, reference)
            VALUES (:agent_id, :amount, 'ggr', 'manual_credit')
        """), {
            "agent_id": agent_id,
            "amount": amount
        })

        conn.commit()

        return {"ok": True, "credited": amount}

    finally:
        conn.close()


# =========================
# AGENT BALANCE
# =========================

@app.get("/admin/ledger/{agent_id}")
def get_agent_balance(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)

    conn = engine.connect()

    try:
        result = conn.execute(text("""
            SELECT COALESCE(SUM(amount),0) as balance
            FROM agent_ledger
            WHERE agent_id = :id
        """), {"id": agent_id}).fetchone()

        return {
            "ok": True,
            "agent_id": agent_id,
            "balance": float(result.balance or 0)
        }

    finally:
        conn.close()


@app.post("/admin/billing/run/{agent_id}")
@app.post("/admin/billing/run/{agent_id}")
def run_billing(agent_id: str, request: Request):
    _require_admin(request.headers.get("X-Admin-Key"))

    with engine.begin() as conn:
        result = conn.execute(text("""
            SELECT
                COALESCE(SUM(amount_usd),0) AS wager,
                COALESCE(SUM(payout),0) AS payout
            FROM dice_bets
            WHERE user_id IN (
                SELECT id FROM users WHERE parent_id = :agent_id
            )
        """), {"agent_id": agent_id}).fetchone()

        wager = float(result.wager or 0)
        payout = float(result.payout or 0)
        ggr = wager - payout

        if ggr <= 0:
            return {"ok": False, "message": "No GGR to process"}

        commission = 0.20
        earnings = ggr * commission

        conn.execute(text("""
            INSERT INTO agent_ledger (agent_id, amount, type, reference)
            VALUES (:agent_id, :amount, ggr_auto, billing_run)
        """), {
            "agent_id": agent_id,
            "amount": earnings
        })

    return {
        "ok": True,
        "agent_id": agent_id,
        "ggr": ggr,
        "commission": commission,
        "credited": earnings
    }


    # commission (default 20%)
    commission = 0.20
    earnings = ggr * commission

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO agent_ledger (agent_id, amount, type, reference)
            VALUES (:agent_id, :amount, 'ggr_auto', 'billing_run')
        """), {
            "agent_id": agent_id,
            "amount": earnings
        })

    return {
        "ok": True,
        "agent_id": agent_id,
        "ggr": ggr,
        "commission": commission,
        "credited": earnings
    }


@app.post("/admin/billing/test/{agent_id}")
def test_billing(agent_id: str, request: Request):
    _require_admin(request.headers.get("X-Admin-Key"))

    # fake GGR for testing
    ggr = 100

    commission = 0.20
    earnings = ggr * commission

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO agent_ledger (agent_id, amount, type, reference)
            VALUES (:agent_id, :amount, 'ggr_auto', 'test_credit')
        """), {
            "agent_id": agent_id,
            "amount": earnings
        })

    return {
        "ok": True,
        "credited": earnings
    }


@app.post("/admin/billing/run-safe/{agent_id}")
def run_billing_safe(agent_id: str, request: Request):
    _require_admin(request.headers.get("X-Admin-Key"))

    # reuse your existing working logic
    ggr_data = get_agent_ggr_full(agent_id, request.headers.get("X-Admin-Key"))
    ggr = ggr_data.get("ggr", 0)

    if ggr <= 0:
        return {"ok": False, "message": "No GGR"}

    commission = 0.20
    earnings = ggr * commission

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO agent_ledger (agent_id, amount, type, reference)
            VALUES (:agent_id, :amount, 'ggr_auto', 'safe_billing')
        """), {
            "agent_id": agent_id,
            "amount": earnings
        })

    return {
        "ok": True,
        "ggr": ggr,
        "credited": earnings
    }


# =========================
# BILLING CONFIG
# =========================

@app.get("/admin/billing/config/{agent_id}")
def get_billing_config(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        row = conn.execute(text("""
            SELECT id, role, billing_type, ggr_share, pph_rate
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": agent_id}).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "ok": True,
            "agent_id": row.id,
            "role": row.role,
            "billing_type": row.billing_type or "ggr",
            "ggr_share": float(row.ggr_share or 0),
            "pph_rate": float(row.pph_rate or 0),
        }
    finally:
        conn.close()


@app.post("/admin/billing/config/{agent_id}")
def set_billing_config(
    agent_id: str,
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    billing_type = str(payload.get("billing_type", "ggr")).strip().lower()
    ggr_share = float(payload.get("ggr_share", 0) or 0)
    pph_rate = float(payload.get("pph_rate", 0) or 0)

    if billing_type not in {"ggr", "pph"}:
        raise HTTPException(status_code=400, detail="billing_type must be 'ggr' or 'pph'")

    if ggr_share < 0 or ggr_share > 1:
        raise HTTPException(status_code=400, detail="ggr_share must be between 0 and 1")

    if pph_rate < 0:
        raise HTTPException(status_code=400, detail="pph_rate must be >= 0")

    conn = engine.connect()
    try:
        exists = conn.execute(text("SELECT id FROM users WHERE id = :id LIMIT 1"), {"id": agent_id}).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="User not found")

        conn.execute(text("""
            UPDATE users
            SET billing_type = :billing_type,
                ggr_share = :ggr_share,
                pph_rate = :pph_rate
            WHERE id = :id
        """), {
            "id": agent_id,
            "billing_type": billing_type,
            "ggr_share": ggr_share,
            "pph_rate": pph_rate,
        })
        conn.commit()

        return {
            "ok": True,
            "agent_id": agent_id,
            "billing_type": billing_type,
            "ggr_share": ggr_share,
            "pph_rate": pph_rate,
        }
    finally:
        conn.close()


@app.post("/admin/billing/run-config/{agent_id}")
def run_billing_config(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        agent = conn.execute(text("""
            SELECT id, billing_type, ggr_share, pph_rate
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": agent_id}).fetchone()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        billing_type = (agent.billing_type or "ggr").lower()
        ggr_share = float(agent.ggr_share or 0)
        pph_rate = float(agent.pph_rate or 0)

        if billing_type == "ggr":
            ggr_data = get_agent_ggr_full(agent_id, x_admin_key)
            ggr = float(ggr_data.get("ggr", 0) or 0)

            if ggr <= 0:
                return {"ok": False, "message": "No GGR", "billing_type": billing_type}

            credited = ggr * ggr_share

            with engine.begin() as tx:
                tx.execute(text("""
                    INSERT INTO agent_ledger (agent_id, amount, type, reference)
                    VALUES (:agent_id, :amount, 'ggr_auto', 'config_billing')
                """), {
                    "agent_id": agent_id,
                    "amount": credited
                })

            return {
                "ok": True,
                "agent_id": agent_id,
                "billing_type": billing_type,
                "ggr": ggr,
                "ggr_share": ggr_share,
                "credited": credited
            }

        if billing_type == "pph":
            downline = _get_all_downline_ids(conn, agent_id)
            player_count = conn.execute(text("""
                SELECT COUNT(*)
                FROM users
                WHERE id = ANY(:ids) AND role = 'player'
            """), {"ids": downline}).scalar() or 0

            if player_count <= 0:
                return {"ok": False, "message": "No players", "billing_type": billing_type}

            credited = float(player_count) * pph_rate

            with engine.begin() as tx:
                tx.execute(text("""
                    INSERT INTO agent_ledger (agent_id, amount, type, reference)
                    VALUES (:agent_id, :amount, 'pph_auto', 'config_billing')
                """), {
                    "agent_id": agent_id,
                    "amount": credited
                })

            return {
                "ok": True,
                "agent_id": agent_id,
                "billing_type": billing_type,
                "player_count": int(player_count),
                "pph_rate": pph_rate,
                "credited": credited
            }

        raise HTTPException(status_code=400, detail="Unsupported billing_type")

    finally:
        conn.close()


# =========================
# MULTI-STREAM BILLING CONFIG
# =========================

@app.get("/admin/billing/multi-config/{agent_id}")
def get_multi_billing_config(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        row = conn.execute(text("""
            SELECT
                id,
                role,
                service_pph,
                service_ggr,
                originals_ggr,
                casino_ggr,
                live_betting_ggr
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": agent_id}).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "ok": True,
            "agent_id": row.id,
            "role": row.role,
            "service_pph": float(row.service_pph or 0),
            "service_ggr": float(row.service_ggr or 0),
            "originals_ggr": float(row.originals_ggr or 0),
            "casino_ggr": float(row.casino_ggr or 0),
            "live_betting_ggr": float(row.live_betting_ggr or 0),
        }
    finally:
        conn.close()


@app.post("/admin/billing/multi-config/{agent_id}")
def set_multi_billing_config(
    agent_id: str,
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    service_pph = float(payload.get("service_pph", 0) or 0)
    service_ggr = float(payload.get("service_ggr", 0) or 0)
    originals_ggr = float(payload.get("originals_ggr", 0) or 0)
    casino_ggr = float(payload.get("casino_ggr", 0) or 0)
    live_betting_ggr = float(payload.get("live_betting_ggr", 0) or 0)

    for name, value in {
        "service_pph": service_pph,
        "service_ggr": service_ggr,
        "originals_ggr": originals_ggr,
        "casino_ggr": casino_ggr,
        "live_betting_ggr": live_betting_ggr,
    }.items():
        if value < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be >= 0")

    conn = engine.connect()
    try:
        exists = conn.execute(
            text("SELECT id FROM users WHERE id = :id LIMIT 1"),
            {"id": agent_id}
        ).fetchone()

        if not exists:
            raise HTTPException(status_code=404, detail="User not found")

        conn.execute(text("""
            UPDATE users
            SET
                service_pph = :service_pph,
                service_ggr = :service_ggr,
                originals_ggr = :originals_ggr,
                casino_ggr = :casino_ggr,
                live_betting_ggr = :live_betting_ggr
            WHERE id = :id
        """), {
            "id": agent_id,
            "service_pph": service_pph,
            "service_ggr": service_ggr,
            "originals_ggr": originals_ggr,
            "casino_ggr": casino_ggr,
            "live_betting_ggr": live_betting_ggr,
        })
        conn.commit()

        return {
            "ok": True,
            "agent_id": agent_id,
            "service_pph": service_pph,
            "service_ggr": service_ggr,
            "originals_ggr": originals_ggr,
            "casino_ggr": casino_ggr,
            "live_betting_ggr": live_betting_ggr,
        }
    finally:
        conn.close()


@app.post("/admin/billing/run-multi/{agent_id}")
def run_multi_billing(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        agent = conn.execute(text("""
            SELECT
                id,
                service_pph,
                service_ggr,
                originals_ggr,
                casino_ggr,
                live_betting_ggr
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": agent_id}).fetchone()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        downline_ids = _get_all_downline_ids(conn, agent_id)

        if not downline_ids:
            return {
                "ok": False,
                "message": "No downline",
                "agent_id": agent_id
            }

        # player count for SERVICE PPH
        player_count = conn.execute(text("""
            SELECT COUNT(*)
            FROM users
            WHERE id = ANY(:ids) AND role = 'player'
        """), {"ids": downline_ids}).scalar() or 0

        # originals = dice + crash
        dice = conn.execute(text("""
            SELECT
                COALESCE(SUM(amount_usd),0) AS wager,
                COALESCE(SUM(payout),0) AS payout
            FROM dice_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        crash = conn.execute(text("""
            SELECT
                COALESCE(SUM(amount_usd),0) AS wager,
                COALESCE(SUM(payout),0) AS payout
            FROM crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        originals_wager = float((dice.wager or 0) + (crash.wager or 0))
        originals_payout = float((dice.payout or 0) + (crash.payout or 0))
        originals_ggr_value = originals_wager - originals_payout

        # placeholders until those tables are wired
        casino_ggr_value = 0.0
        live_betting_ggr_value = 0.0

        service_pph_charge = float(player_count) * float(agent.service_pph or 0)
        service_ggr_charge = originals_ggr_value * float(agent.service_ggr or 0)
        originals_ggr_charge = originals_ggr_value * float(agent.originals_ggr or 0)
        casino_ggr_charge = casino_ggr_value * float(agent.casino_ggr or 0)
        live_betting_ggr_charge = live_betting_ggr_value * float(agent.live_betting_ggr or 0)

        total_charge = (
            service_pph_charge
            + service_ggr_charge
            + originals_ggr_charge
            + casino_ggr_charge
            + live_betting_ggr_charge
        )

        if total_charge <= 0:
            return {
                "ok": False,
                "message": "No billable amount",
                "agent_id": agent_id,
                "player_count": int(player_count),
                "originals_ggr_value": originals_ggr_value
            }

        with engine.begin() as tx:
            tx.execute(text("""
                INSERT INTO agent_ledger (agent_id, amount, type, reference)
                VALUES (:agent_id, :amount, 'multi_billing', 'multi_stream_billing')
            """), {
                "agent_id": agent_id,
                "amount": total_charge
            })

        return {
            "ok": True,
            "agent_id": agent_id,
            "player_count": int(player_count),

            "service_pph_rate": float(agent.service_pph or 0),
            "service_pph_charge": service_pph_charge,

            "service_ggr_rate": float(agent.service_ggr or 0),
            "service_ggr_charge": service_ggr_charge,

            "originals_ggr_rate": float(agent.originals_ggr or 0),
            "originals_ggr_value": originals_ggr_value,
            "originals_ggr_charge": originals_ggr_charge,

            "casino_ggr_rate": float(agent.casino_ggr or 0),
            "casino_ggr_value": casino_ggr_value,
            "casino_ggr_charge": casino_ggr_charge,

            "live_betting_ggr_rate": float(agent.live_betting_ggr or 0),
            "live_betting_ggr_value": live_betting_ggr_value,
            "live_betting_ggr_charge": live_betting_ggr_charge,

            "total_charge": total_charge
        }
    finally:
        conn.close()


import json
from datetime import datetime, timezone

def _billing_period_key():
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week}"

@app.get("/admin/billing/history/{agent_id}")
def get_billing_history(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        rows = conn.execute(text("""
            SELECT id, agent_id, billing_mode, period_key, total_amount, details_json, created_at
            FROM billing_runs
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 50
        """), {"agent_id": agent_id}).fetchall()

        return {
            "ok": True,
            "agent_id": agent_id,
            "history": [
                {
                    "id": r.id,
                    "billing_mode": r.billing_mode,
                    "period_key": r.period_key,
                    "total_amount": float(r.total_amount or 0),
                    "details_json": r.details_json,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/admin/billing/run-multi-protected/{agent_id}")
def run_multi_billing_protected(
    agent_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        period_key = _billing_period_key()
        billing_mode = "multi_stream"

        existing = conn.execute(text("""
            SELECT id, total_amount, created_at
            FROM billing_runs
            WHERE agent_id = :agent_id
              AND billing_mode = :billing_mode
              AND period_key = :period_key
            LIMIT 1
        """), {
            "agent_id": agent_id,
            "billing_mode": billing_mode,
            "period_key": period_key
        }).fetchone()

        if existing:
            return {
                "ok": False,
                "message": "Billing already run for this period",
                "agent_id": agent_id,
                "billing_mode": billing_mode,
                "period_key": period_key,
                "existing_run_id": existing.id,
                "existing_amount": float(existing.total_amount or 0),
            }

        agent = conn.execute(text("""
            SELECT
                id,
                service_pph,
                service_ggr,
                originals_ggr,
                casino_ggr,
                live_betting_ggr
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": agent_id}).fetchone()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        downline_ids = _get_all_downline_ids(conn, agent_id)

        if not downline_ids:
            return {
                "ok": False,
                "message": "No downline",
                "agent_id": agent_id,
                "period_key": period_key
            }

        player_count = conn.execute(text("""
            SELECT COUNT(*)
            FROM users
            WHERE id = ANY(:ids) AND role = 'player'
        """), {"ids": downline_ids}).scalar() or 0

        dice = conn.execute(text("""
            SELECT
                COALESCE(SUM(amount_usd),0) AS wager,
                COALESCE(SUM(payout),0) AS payout
            FROM dice_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        crash = conn.execute(text("""
            SELECT
                COALESCE(SUM(amount_usd),0) AS wager,
                COALESCE(SUM(payout),0) AS payout
            FROM crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": downline_ids}).fetchone()

        originals_wager = float((dice.wager or 0) + (crash.wager or 0))
        originals_payout = float((dice.payout or 0) + (crash.payout or 0))
        originals_ggr_value = originals_wager - originals_payout

        casino_ggr_value = 0.0
        live_betting_ggr_value = 0.0

        service_pph_charge = float(player_count) * float(agent.service_pph or 0)
        service_ggr_charge = originals_ggr_value * float(agent.service_ggr or 0)
        originals_ggr_charge = originals_ggr_value * float(agent.originals_ggr or 0)
        casino_ggr_charge = casino_ggr_value * float(agent.casino_ggr or 0)
        live_betting_ggr_charge = live_betting_ggr_value * float(agent.live_betting_ggr or 0)

        total_charge = (
            service_pph_charge
            + service_ggr_charge
            + originals_ggr_charge
            + casino_ggr_charge
            + live_betting_ggr_charge
        )

        if total_charge <= 0:
            return {
                "ok": False,
                "message": "No billable amount",
                "agent_id": agent_id,
                "period_key": period_key,
                "player_count": int(player_count),
                "originals_ggr_value": originals_ggr_value
            }

        details = {
            "player_count": int(player_count),
            "service_pph_rate": float(agent.service_pph or 0),
            "service_pph_charge": service_pph_charge,
            "service_ggr_rate": float(agent.service_ggr or 0),
            "service_ggr_charge": service_ggr_charge,
            "originals_ggr_rate": float(agent.originals_ggr or 0),
            "originals_ggr_value": originals_ggr_value,
            "originals_ggr_charge": originals_ggr_charge,
            "casino_ggr_rate": float(agent.casino_ggr or 0),
            "casino_ggr_value": casino_ggr_value,
            "casino_ggr_charge": casino_ggr_charge,
            "live_betting_ggr_rate": float(agent.live_betting_ggr or 0),
            "live_betting_ggr_value": live_betting_ggr_value,
            "live_betting_ggr_charge": live_betting_ggr_charge,
            "total_charge": total_charge,
        }

        with engine.begin() as tx:
            tx.execute(text("""
                INSERT INTO agent_ledger (agent_id, amount, type, reference)
                VALUES (:agent_id, :amount, 'multi_billing', :reference)
            """), {
                "agent_id": agent_id,
                "amount": total_charge,
                "reference": f"multi_stream_billing:{period_key}"
            })

            tx.execute(text("""
                INSERT INTO billing_runs (agent_id, billing_mode, period_key, total_amount, details_json)
                VALUES (:agent_id, :billing_mode, :period_key, :total_amount, :details_json)
            """), {
                "agent_id": agent_id,
                "billing_mode": billing_mode,
                "period_key": period_key,
                "total_amount": total_charge,
                "details_json": json.dumps(details)
            })

        return {
            "ok": True,
            "agent_id": agent_id,
            "billing_mode": billing_mode,
            "period_key": period_key,
            **details
        }

    finally:
        conn.close()


# =========================
# BRAND / DOMAIN MAPPING
# =========================

@app.get("/admin/brands")
def list_brands(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        rows = conn.execute(text("""
            SELECT
                id,
                owner_user_id,
                brand_name,
                domain,
                logo_url,
                primary_color,
                secondary_color,
                support_email,
                support_telegram,
                is_active,
                created_at
            FROM brand_domains
            ORDER BY created_at DESC
        """)).fetchall()

        return {
            "ok": True,
            "brands": [
                {
                    "id": r.id,
                    "owner_user_id": r.owner_user_id,
                    "brand_name": r.brand_name,
                    "domain": r.domain,
                    "logo_url": r.logo_url,
                    "primary_color": r.primary_color,
                    "secondary_color": r.secondary_color,
                    "support_email": r.support_email,
                    "support_telegram": r.support_telegram,
                    "is_active": bool(r.is_active),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.get("/admin/brands/{domain}")
def get_brand_by_domain(
    domain: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        row = conn.execute(text("""
            SELECT
                id,
                owner_user_id,
                brand_name,
                domain,
                logo_url,
                primary_color,
                secondary_color,
                support_email,
                support_telegram,
                is_active,
                created_at
            FROM brand_domains
            WHERE lower(domain) = lower(:domain)
            LIMIT 1
        """), {"domain": domain}).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Brand not found")

        return {
            "ok": True,
            "brand": {
                "id": row.id,
                "owner_user_id": row.owner_user_id,
                "brand_name": row.brand_name,
                "domain": row.domain,
                "logo_url": row.logo_url,
                "primary_color": row.primary_color,
                "secondary_color": row.secondary_color,
                "support_email": row.support_email,
                "support_telegram": row.support_telegram,
                "is_active": bool(row.is_active),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        }
    finally:
        conn.close()


@app.post("/admin/brands")
def create_brand(
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    owner_user_id = str(payload.get("owner_user_id", "")).strip()
    brand_name = str(payload.get("brand_name", "")).strip()
    domain = str(payload.get("domain", "")).strip().lower()
    logo_url = payload.get("logo_url")
    primary_color = payload.get("primary_color")
    secondary_color = payload.get("secondary_color")
    support_email = payload.get("support_email")
    support_telegram = payload.get("support_telegram")
    is_active = bool(payload.get("is_active", True))

    if not owner_user_id or not brand_name or not domain:
        raise HTTPException(status_code=400, detail="owner_user_id, brand_name, and domain are required")

    conn = engine.connect()
    try:
        owner = conn.execute(text("""
            SELECT id, role
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": owner_user_id}).fetchone()

        if not owner:
            raise HTTPException(status_code=404, detail="Owner user not found")

        if str(owner.role or "") not in {"super_admin", "admin", "master_agent", "agent"}:
            raise HTTPException(status_code=400, detail="This role cannot own a brand/domain")

        conn.execute(text("""
            INSERT INTO brand_domains (
                owner_user_id,
                brand_name,
                domain,
                logo_url,
                primary_color,
                secondary_color,
                support_email,
                support_telegram,
                is_active
            )
            VALUES (
                :owner_user_id,
                :brand_name,
                :domain,
                :logo_url,
                :primary_color,
                :secondary_color,
                :support_email,
                :support_telegram,
                :is_active
            )
        """), {
            "owner_user_id": owner_user_id,
            "brand_name": brand_name,
            "domain": domain,
            "logo_url": logo_url,
            "primary_color": primary_color,
            "secondary_color": secondary_color,
            "support_email": support_email,
            "support_telegram": support_telegram,
            "is_active": is_active,
        })
        conn.commit()

        return {
            "ok": True,
            "owner_user_id": owner_user_id,
            "brand_name": brand_name,
            "domain": domain,
            "is_active": is_active,
        }
    finally:
        conn.close()


@app.post("/admin/brands/update/{brand_id}")
def update_brand(
    brand_id: int,
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        row = conn.execute(text("SELECT id FROM brand_domains WHERE id = :id LIMIT 1"), {"id": brand_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Brand not found")

        conn.execute(text("""
            UPDATE brand_domains
            SET
                owner_user_id = COALESCE(:owner_user_id, owner_user_id),
                brand_name = COALESCE(:brand_name, brand_name),
                domain = COALESCE(:domain, domain),
                logo_url = COALESCE(:logo_url, logo_url),
                primary_color = COALESCE(:primary_color, primary_color),
                secondary_color = COALESCE(:secondary_color, secondary_color),
                support_email = COALESCE(:support_email, support_email),
                support_telegram = COALESCE(:support_telegram, support_telegram),
                is_active = COALESCE(:is_active, is_active)
            WHERE id = :id
        """), {
            "id": brand_id,
            "owner_user_id": payload.get("owner_user_id"),
            "brand_name": payload.get("brand_name"),
            "domain": str(payload.get("domain")).lower() if payload.get("domain") else None,
            "logo_url": payload.get("logo_url"),
            "primary_color": payload.get("primary_color"),
            "secondary_color": payload.get("secondary_color"),
            "support_email": payload.get("support_email"),
            "support_telegram": payload.get("support_telegram"),
            "is_active": payload.get("is_active"),
        })
        conn.commit()

        return {"ok": True, "brand_id": brand_id}
    finally:
        conn.close()


@app.get("/public/brand-by-host")
def public_brand_by_host(host: str):
    conn = engine.connect()
    try:
        row = conn.execute(text("""
            SELECT
                owner_user_id,
                brand_name,
                domain,
                logo_url,
                primary_color,
                secondary_color,
                support_email,
                support_telegram,
                is_active
            FROM brand_domains
            WHERE lower(domain) = lower(:host)
              AND is_active = true
            LIMIT 1
        """), {"host": host}).fetchone()

        if not row:
            return {"ok": False, "brand": None}

        return {
            "ok": True,
            "brand": {
                "owner_user_id": row.owner_user_id,
                "brand_name": row.brand_name,
                "domain": row.domain,
                "logo_url": row.logo_url,
                "primary_color": row.primary_color,
                "secondary_color": row.secondary_color,
                "support_email": row.support_email,
                "support_telegram": row.support_telegram,
                "is_active": bool(row.is_active),
            }
        }
    finally:
        conn.close()


@app.get("/admin/users")
def admin_users(limit: int = 200, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    conn = engine.connect()
    try:
        rows = conn.execute(text("""
            SELECT
                id,
                role,
                parent_id,
                created_by,
                agent_code,
                is_active,
                created_at,
                billing_type,
                pph_rate,
                ggr_share,
                service_pph,
                service_ggr,
                originals_ggr,
                casino_ggr,
                live_betting_ggr
            FROM users
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()

        return {
            "ok": True,
            "count": len(rows),
            "users": [
                {
                    "id": r.id,
                    "role": r.role,
                    "parent_id": r.parent_id,
                    "created_by": r.created_by,
                    "agent_code": r.agent_code,
                    "is_active": bool(r.is_active) if r.is_active is not None else True,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "billing_type": r.billing_type,
                    "pph_rate": float(r.pph_rate or 0),
                    "ggr_share": float(r.ggr_share or 0),
                    "service_pph": float(r.service_pph or 0),
                    "service_ggr": float(r.service_ggr or 0),
                    "originals_ggr": float(r.originals_ggr or 0),
                    "casino_ggr": float(r.casino_ggr or 0),
                    "live_betting_ggr": float(r.live_betting_ggr or 0),
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/admin/users/{user_id}/disable")
def admin_disable_user(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    conn = engine.connect()
    try:
        exists = conn.execute(text("SELECT id FROM users WHERE id = :id LIMIT 1"), {"id": user_id}).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="User not found")

        conn.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id})
        conn.commit()
        return {"ok": True, "user_id": user_id, "is_active": False}
    finally:
        conn.close()


@app.post("/admin/users/{user_id}/enable")
def admin_enable_user(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    conn = engine.connect()
    try:
        exists = conn.execute(text("SELECT id FROM users WHERE id = :id LIMIT 1"), {"id": user_id}).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="User not found")

        conn.execute(text("UPDATE users SET is_active = true WHERE id = :id"), {"id": user_id})
        conn.commit()
        return {"ok": True, "user_id": user_id, "is_active": True}
    finally:
        conn.close()


def _collect_descendants(conn, root_user_id: str) -> set[str]:
    descendants = set()
    stack = [root_user_id]

    while stack:
        current = stack.pop()
        rows = conn.execute(text("""
            SELECT id
            FROM users
            WHERE parent_id = :parent_id
        """), {"parent_id": current}).fetchall()

        for r in rows:
            child_id = str(r.id)
            if child_id not in descendants:
                descendants.add(child_id)
                stack.append(child_id)

    return descendants


@app.post("/admin/users/{user_id}/update-metadata")
def admin_update_user_metadata(
    user_id: str,
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    actor = str(payload.get("updated_by") or "admin").strip()
    new_parent_id = payload.get("parent_id")
    new_agent_code = payload.get("agent_code")
    new_created_by = payload.get("created_by")

    conn = engine.connect()
    try:
        current = conn.execute(text("""
            SELECT id, role, parent_id, agent_code, created_by
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": user_id}).fetchone()

        if not current:
            raise HTTPException(status_code=404, detail="User not found")

        updates = []
        params = {"id": user_id, "updated_by": actor}

        if new_parent_id is not None:
            new_parent_id = str(new_parent_id).strip() or None

            if new_parent_id == user_id:
                raise HTTPException(status_code=400, detail="User cannot be their own parent")

            if new_parent_id:
                parent = conn.execute(text("""
                    SELECT id, role
                    FROM users
                    WHERE id = :id
                    LIMIT 1
                """), {"id": new_parent_id}).fetchone()

                if not parent:
                    raise HTTPException(status_code=404, detail="New parent not found")

                descendants = _collect_descendants(conn, user_id)
                if new_parent_id in descendants:
                    raise HTTPException(status_code=400, detail="Cannot move user under their own descendant")

            if str(current.parent_id or "") != str(new_parent_id or ""):
                updates.append("parent_id = :parent_id")
                updates.append("last_parent_change_at = NOW()")
                updates.append("last_parent_change_by = :updated_by")
                params["parent_id"] = new_parent_id

        if new_agent_code is not None:
            new_agent_code = str(new_agent_code).strip() or None
            if str(current.agent_code or "") != str(new_agent_code or ""):
                updates.append("agent_code = :agent_code")
                updates.append("last_agent_code_change_at = NOW()")
                updates.append("last_agent_code_change_by = :updated_by")
                params["agent_code"] = new_agent_code

        if new_created_by is not None:
            new_created_by = str(new_created_by).strip() or None
            if str(current.created_by or "") != str(new_created_by or ""):
                updates.append("created_by = :created_by")
                updates.append("last_created_by_change_at = NOW()")
                updates.append("last_created_by_change_by = :updated_by")
                params["created_by"] = new_created_by

        if not updates:
            return {"ok": True, "message": "No changes detected", "user_id": user_id}

        updates.append("updated_at = NOW()")
        updates.append("updated_by = :updated_by")

        conn.execute(text(f"""
            UPDATE users
            SET {", ".join(updates)}
            WHERE id = :id
        """), params)
        conn.commit()

        updated = conn.execute(text("""
            SELECT
                id,
                role,
                parent_id,
                agent_code,
                created_by,
                updated_at,
                updated_by,
                last_parent_change_at,
                last_parent_change_by,
                last_agent_code_change_at,
                last_agent_code_change_by,
                last_created_by_change_at,
                last_created_by_change_by
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": user_id}).fetchone()

        return {
            "ok": True,
            "user": {
                "id": updated.id,
                "role": updated.role,
                "parent_id": updated.parent_id,
                "agent_code": updated.agent_code,
                "created_by": updated.created_by,
                "updated_at": updated.updated_at.isoformat() if updated.updated_at else None,
                "updated_by": updated.updated_by,
                "last_parent_change_at": updated.last_parent_change_at.isoformat() if updated.last_parent_change_at else None,
                "last_parent_change_by": updated.last_parent_change_by,
                "last_agent_code_change_at": updated.last_agent_code_change_at.isoformat() if updated.last_agent_code_change_at else None,
                "last_agent_code_change_by": updated.last_agent_code_change_by,
                "last_created_by_change_at": updated.last_created_by_change_at.isoformat() if updated.last_created_by_change_at else None,
                "last_created_by_change_by": updated.last_created_by_change_by,
            }
        }
    finally:
        conn.close()




@app.post("/admin/users/{user_id}/wallet-adjust")
async def admin_user_wallet_adjust(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a valid number")

    reason = str(body.get("reason", "")).strip() or "manual_profile_adjustment"

    if amount == 0:
        raise HTTPException(status_code=400, detail="amount must not be 0")

    db = SessionLocal()
    try:
        w = _get_or_create_user_and_wallet(db, user_id)

        new_total = _round2(float(w.balance_total) + amount)
        new_available = _round2(float(w.balance_available) + amount)

        if new_total < 0 or new_available < 0:
            raise HTTPException(status_code=400, detail="Adjustment would make balance negative")

        w.balance_total = new_total
        w.balance_available = new_available

        tx = Transaction(
            user_id=user_id,
            type="admin_adjustment",
            amount=float(amount),
            balance_after=float(w.balance_total),
            reference=reason,
        )
        db.add(tx)
        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "amount": float(amount),
            "reason": reason,
            "wallet": _serialize_wallet(w),
        }
    finally:
        db.close()


@app.post("/admin/users/{user_id}/update-contact")
async def admin_user_update_contact(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    body = await request.json()

    full_name = body.get("full_name")
    telegram = body.get("telegram")
    phone = body.get("phone")
    notes = body.get("notes")

    conn = engine.begin()
    with conn as tx:
        exists = tx.execute(text("""
            SELECT user_id
            FROM c2w_users
            WHERE user_id = :id
            LIMIT 1
        """), {"id": user_id}).fetchone()

        if not exists:
            raise HTTPException(status_code=404, detail="Auth user not found")

        tx.execute(text("""
            UPDATE c2w_users
            SET
                full_name = :full_name,
                telegram = :telegram,
                phone = :phone,
                notes = :notes
            WHERE user_id = :id
        """), {
            "id": user_id,
            "full_name": (str(full_name).strip() if full_name is not None else None) or None,
            "telegram": (str(telegram).strip() if telegram is not None else None) or None,
            "phone": (str(phone).strip() if phone is not None else None) or None,
            "notes": (str(notes).strip() if notes is not None else None) or None,
        })

    return {
        "ok": True,
        "user_id": user_id,
        "updated": True,
    }



@app.get("/admin/crm/low-balance/{viewer_id}")
def admin_crm_low_balance(
    viewer_id: str,
    threshold: float = 10.0,
    limit: int = 100,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        viewer = conn.execute(text("""
            SELECT id, role
            FROM users
            WHERE id = :id
            LIMIT 1
        """), {"id": viewer_id}).fetchone()

        if not viewer:
            raise HTTPException(status_code=404, detail="Viewer not found")

        viewer_role = str(viewer.role or "").strip().lower()
        if viewer_role not in {"super_admin", "admin", "master_agent", "agent", "sub_agent"}:
            raise HTTPException(status_code=403, detail="CRM access not allowed for this role")

        if viewer_role in {"super_admin", "admin"}:
            rows = conn.execute(text("""
                SELECT
                    u.id AS user_id,
                    u.parent_id,
                    u.role,
                    u.is_active,
                    a.username,
                    a.full_name,
                    a.email,
                    a.telegram,
                    w.balance_available,
                    w.balance_total,
                    w.balance_pending
                FROM users u
                LEFT JOIN wallets w ON w.user_id = u.id
                LEFT JOIN c2w_users a ON a.user_id = u.id
                WHERE u.role = 'player'
                  AND COALESCE(u.is_active, TRUE) = TRUE
                  AND COALESCE(w.balance_available, 0) <= :threshold
                ORDER BY COALESCE(w.balance_available, 0) ASC, u.created_at DESC
                LIMIT :limit
            """), {
                "threshold": threshold,
                "limit": max(1, min(limit, 500)),
            }).fetchall()
            scope = "global"
        else:
            rows = conn.execute(text("""
                WITH RECURSIVE user_tree AS (
                    SELECT id
                    FROM users
                    WHERE id = :viewer_id

                    UNION ALL

                    SELECT u.id
                    FROM users u
                    INNER JOIN user_tree ut ON u.parent_id = ut.id
                )
                SELECT
                    u.id AS user_id,
                    u.parent_id,
                    u.role,
                    u.is_active,
                    a.username,
                    a.full_name,
                    a.email,
                    a.telegram,
                    w.balance_available,
                    w.balance_total,
                    w.balance_pending
                FROM users u
                INNER JOIN user_tree ut ON ut.id = u.id
                LEFT JOIN wallets w ON w.user_id = u.id
                LEFT JOIN c2w_users a ON a.user_id = u.id
                WHERE u.role = 'player'
                  AND COALESCE(u.is_active, TRUE) = TRUE
                  AND COALESCE(w.balance_available, 0) <= :threshold
                ORDER BY COALESCE(w.balance_available, 0) ASC, u.created_at DESC
                LIMIT :limit
            """), {
                "viewer_id": viewer_id,
                "threshold": threshold,
                "limit": max(1, min(limit, 500)),
            }).fetchall()
            scope = "subtree"

        items = []
        for r in rows:
            items.append({
                "user_id": r.user_id,
                "parent_id": r.parent_id,
                "role": r.role,
                "is_active": bool(r.is_active) if r.is_active is not None else True,
                "username": r.username,
                "full_name": r.full_name,
                "email": r.email,
                "telegram": r.telegram,
                "balance_available": float(r.balance_available or 0),
                "balance_total": float(r.balance_total or 0),
                "balance_pending": float(r.balance_pending or 0),
                "trigger_type": "low_balance",
                "suggested_reason": "loss_rebate",
            })

        return {
            "ok": True,
            "viewer_id": viewer_id,
            "viewer_role": viewer_role,
            "scope": scope,
            "threshold": float(threshold),
            "count": len(items),
            "items": items,
        }
    finally:
        conn.close()


@app.get("/admin/users/{user_id}/profile")
def admin_user_profile(
    user_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    conn = engine.connect()
    try:
        user = conn.execute(text("""
            SELECT
                u.id,
                u.role,
                u.parent_id,
                u.created_by,
                u.agent_code,
                u.is_active,
                u.created_at,
                u.kyc_status,
                u.kyc_level,
                u.auto_withdraw_enabled,
                u.auto_withdraw_limit,
                u.kyc_verified_at,
                u.kyc_rejected_reason,
                u.billing_type,
                u.pph_rate,
                u.ggr_share,
                u.service_pph,
                u.service_ggr,
                u.originals_ggr,
                u.casino_ggr,
                u.live_betting_ggr,
                u.updated_at,
                u.updated_by,
                u.last_parent_change_at,
                u.last_parent_change_by,
                u.last_agent_code_change_at,
                u.last_agent_code_change_by,
                u.last_created_by_change_at,
                u.last_created_by_change_by,
                a.email AS auth_email,
                a.username AS auth_username,
                a.full_name AS auth_full_name,
                a.telegram AS auth_telegram,
                a.phone AS auth_phone,
                a.notes AS auth_notes
            FROM users u
            LEFT JOIN c2w_users a ON a.user_id = u.id
            WHERE u.id = :id
            LIMIT 1
        """), {"id": user_id}).fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        wallet = conn.execute(text("""
            SELECT
                balance_total,
                balance_available,
                balance_pending,
                updated_at
            FROM wallets
            WHERE user_id = :id
            LIMIT 1
        """), {"id": user_id}).fetchone()

        deposit_stats = conn.execute(text("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount_usd), 0) AS total_amount,
                COALESCE(SUM(CASE WHEN status = 'credited' THEN amount_usd ELSE 0 END), 0) AS credited_amount
            FROM deposits
            WHERE user_id = :id
        """), {"id": user_id}).fetchone()

        withdrawal_stats = conn.execute(text("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount_usd), 0) AS total_amount,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN amount_usd ELSE 0 END), 0) AS completed_amount,
                COALESCE(SUM(CASE WHEN status IN ('requested','approved','sent') THEN amount_usd ELSE 0 END), 0) AS pending_amount
            FROM withdrawals
            WHERE user_id = :id
        """), {"id": user_id}).fetchone()

        recent_transactions = conn.execute(text("""
            SELECT
                id,
                type,
                amount,
                balance_after,
                reference,
                created_at
            FROM transactions
            WHERE user_id = :id
            ORDER BY created_at DESC
            LIMIT 20
        """), {"id": user_id}).fetchall()

        recent_deposits = conn.execute(text("""
            SELECT
                id,
                payment_id,
                status,
                amount_usd,
                pay_currency,
                pay_amount,
                pay_address,
                created_at,
                updated_at
            FROM deposits
            WHERE user_id = :id
            ORDER BY created_at DESC
            LIMIT 10
        """), {"id": user_id}).fetchall()

        recent_withdrawals = conn.execute(text("""
            SELECT
                id,
                status,
                amount_usd,
                payout_currency,
                payout_address,
                refunded,
                note,
                created_at,
                updated_at
            FROM withdrawals
            WHERE user_id = :id
            ORDER BY created_at DESC
            LIMIT 10
        """), {"id": user_id}).fetchall()

        return {
            "ok": True,
            "profile": {
                "user": {
                    "id": user.id,
                    "email": user.auth_email,
                    "username": user.auth_username,
                    "full_name": user.auth_full_name,
                    "telegram": user.auth_telegram,
                    "phone": user.auth_phone,
                    "notes": user.auth_notes,
                    "role": user.role,
                    "parent_id": user.parent_id,
                    "created_by": user.created_by,
                    "agent_code": user.agent_code,
                    "is_active": bool(user.is_active) if user.is_active is not None else True,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "kyc_status": user.kyc_status,
                    "kyc_level": int(user.kyc_level or 0),
                    "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
                    "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
                    "kyc_verified_at": user.kyc_verified_at.isoformat() if user.kyc_verified_at else None,
                    "kyc_rejected_reason": user.kyc_rejected_reason,
                    "billing_type": user.billing_type,
                    "pph_rate": float(user.pph_rate or 0),
                    "ggr_share": float(user.ggr_share or 0),
                    "service_pph": float(user.service_pph or 0),
                    "service_ggr": float(user.service_ggr or 0),
                    "originals_ggr": float(user.originals_ggr or 0),
                    "casino_ggr": float(user.casino_ggr or 0),
                    "live_betting_ggr": float(user.live_betting_ggr or 0),
                    "updated_at": user.updated_at.isoformat() if user.updated_at else None,
                    "updated_by": user.updated_by,
                    "last_parent_change_at": user.last_parent_change_at.isoformat() if user.last_parent_change_at else None,
                    "last_parent_change_by": user.last_parent_change_by,
                    "last_agent_code_change_at": user.last_agent_code_change_at.isoformat() if user.last_agent_code_change_at else None,
                    "last_agent_code_change_by": user.last_agent_code_change_by,
                    "last_created_by_change_at": user.last_created_by_change_at.isoformat() if user.last_created_by_change_at else None,
                    "last_created_by_change_by": user.last_created_by_change_by,
                },
                "wallet": {
                    "balance_total": float(wallet.balance_total or 0) if wallet else 0,
                    "balance_available": float(wallet.balance_available or 0) if wallet else 0,
                    "balance_pending": float(wallet.balance_pending or 0) if wallet else 0,
                    "updated_at": wallet.updated_at.isoformat() if wallet and wallet.updated_at else None,
                },
                "deposit_stats": {
                    "total_count": int(deposit_stats.total_count or 0),
                    "total_amount": float(deposit_stats.total_amount or 0),
                    "credited_amount": float(deposit_stats.credited_amount or 0),
                },
                "withdrawal_stats": {
                    "total_count": int(withdrawal_stats.total_count or 0),
                    "total_amount": float(withdrawal_stats.total_amount or 0),
                    "completed_amount": float(withdrawal_stats.completed_amount or 0),
                    "pending_amount": float(withdrawal_stats.pending_amount or 0),
                },
                "recent_transactions": [
                    {
                        "id": r.id,
                        "type": r.type,
                        "amount": float(r.amount or 0),
                        "balance_after": float(r.balance_after or 0),
                        "reference": r.reference,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in recent_transactions
                ],
                "recent_deposits": [
                    {
                        "id": r.id,
                        "payment_id": r.payment_id,
                        "status": r.status,
                        "amount_usd": float(r.amount_usd or 0),
                        "pay_currency": r.pay_currency,
                        "pay_amount": float(r.pay_amount or 0) if r.pay_amount is not None else None,
                        "pay_address": r.pay_address,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in recent_deposits
                ],
                "recent_withdrawals": [
                    {
                        "id": r.id,
                        "status": r.status,
                        "amount_usd": float(r.amount_usd or 0),
                        "payout_currency": r.payout_currency,
                        "payout_address": r.payout_address,
                        "refunded": bool(r.refunded),
                        "note": r.note,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in recent_withdrawals
                ],
            }
        }
    finally:
        conn.close()


@app.get("/deposit/{user_id}")
def deposit_list_user(user_id: str, limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(Deposit)
            .filter(Deposit.user_id == user_id)
            .order_by(Deposit.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return {
            "user_id": user_id,
            "count": len(rows),
            "deposits": [_serialize_deposit(d) for d in rows]
        }
    finally:
        db.close()

@app.post("/admin/wallet/adjust")
async def admin_wallet_adjust(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    user_id = str(body.get("user_id", "")).strip()
    reason = str(body.get("reason", "")).strip() or "admin_adjustment"

    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a valid number")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount == 0:
        raise HTTPException(status_code=400, detail="amount must not be 0")

    db = SessionLocal()
    try:
        w = _get_or_create_user_and_wallet(db, user_id)

        w.balance_total = _round2(float(w.balance_total) + amount)
        w.balance_available = _round2(float(w.balance_available) + amount)

        tx = Transaction(
            user_id=user_id,
            type="admin_adjustment",
            amount=float(amount),
            balance_after=float(w.balance_total),
            reference=reason,
        )
        db.add(tx)
        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "amount": float(amount),
            "reason": reason,
            "wallet": _serialize_wallet(w),
        }
    finally:
        db.close()


import requests
import os

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYOUTS_PAYOUT_API = os.getenv("NOWPAYOUTS_PAYOUT_API")

@app.post("/admin/withdrawals/{withdrawal_id}/send")
async def admin_withdrawal_send(withdrawal_id: int, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status != "approved":
            raise HTTPException(status_code=400, detail="Withdrawal not approved")
        payload = {
            "ipn_callback_url": os.getenv("NOWPAYMENTS_IPN_CALLBACK_URL"),
            "withdrawals": [{
                "address": wd.payout_address,
                "currency": wd.payout_currency,
                "amount": wd.amount_usd
            }]
        }
        headers = {
            "Authorization": f"Bearer {NOWPAYMENTS_API_KEY}",
            "Content-Type": "application/json"
        }
        r = requests.post(NOWPAYOUTS_PAYOUT_API, json=payload, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail=f"NOWPayments error: {r.text}")
        wd.status = "sent"
        db.commit()
        return {"ok": True, "withdrawal_id": wd.id, "status": wd.status, "np": r.json()}
    finally:
        db.close()



# =========================
# NOWPAYMENTS REAL PAYOUT FLOW (SEMI-AUTO)
# CTO REVIEW:
# - SOLO usdt/usdttrc20 por ahora
# - btc/ltc quedan manual hasta implementar quote/conversion segura
# =========================

NOWPAYOUTS_AUTH_API = os.getenv("NOWPAYOUTS_AUTH_API", "https://api.nowpayments.io/v1/auth")

def _np_payout_jwt():
    r = requests.post(
        NOWPAYOUTS_AUTH_API,
        json={
            "email": os.getenv("NOWPAYOUTS_EMAIL"),
            "password": os.getenv("NOWPAYOUTS_PASSWORD"),
        },
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"NOWPayments auth error: {r.text}")
    data = r.json()
    token = data.get("token") or data.get("jwt") or data.get("access_token")
    if not token:
        raise HTTPException(status_code=500, detail=f"NOWPayments auth missing token: {data}")
    return token

def _np_headers(jwt_token: str):
    return {
        "x-api-key": os.getenv("NOWPAYMENTS_API_KEY", ""),
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }

def _normalize_payout_currency(cur: str):
    c = str(cur or "").strip().lower()
    if c == "usdt":
        return "usdttrc20"
    return c

@app.post("/admin/withdrawals/{withdrawal_id}/create-payout")
async def admin_withdrawal_create_payout(
    withdrawal_id: int,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status != "approved":
            raise HTTPException(status_code=400, detail="Withdrawal not approved")

        currency = _normalize_payout_currency(wd.payout_currency)

        # CTO SAFETY: solo USDT TRC20 real por ahora
        if currency != "usdttrc20":
            raise HTTPException(status_code=400, detail="Auto payout real solo habilitado para USDT TRC20 por ahora")

        jwt_token = _np_payout_jwt()

        payload = {
            "ipn_callback_url": os.getenv("NOWPAYMENTS_IPN_CALLBACK_URL"),
            "withdrawals": [
                {
                    "address": wd.payout_address,
                    "currency": currency,
                    "amount": float(wd.amount_usd),
                    "ipn_callback_url": os.getenv("NOWPAYMENTS_IPN_CALLBACK_URL"),
                }
            ]
        }

        r = requests.post(
            os.getenv("NOWPAYOUTS_PAYOUT_API", "https://api.nowpayments.io/v1/payout"),
            json=payload,
            headers=_np_headers(jwt_token),
            timeout=30,
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"NOWPayments create payout error: {r.text}")

        data = r.json()
        batch_id = data.get("id") or data.get("batch_withdrawal_id")
        if not batch_id:
            raise HTTPException(status_code=500, detail=f"NOWPayments create payout missing batch id: {data}")

        wd.batch_withdrawal_id = str(batch_id)
        wd.payout_status = "created"
        wd.payout_response_json = json.dumps(data)
        wd.status = "approved"  # NO marcar sent todavía
        db.commit()

        return {
            "ok": True,
            "withdrawal_id": wd.id,
            "status": wd.status,
            "batch_withdrawal_id": wd.batch_withdrawal_id,
            "payout_status": wd.payout_status,
            "np": data,
        }
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/verify-payout")
async def admin_withdrawal_verify_payout(
    withdrawal_id: int,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    body = await request.json()
    verification_code = str(body.get("verification_code", "")).strip()

    if not verification_code:
        raise HTTPException(status_code=400, detail="verification_code required")

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if not wd.batch_withdrawal_id:
            raise HTTPException(status_code=400, detail="Payout batch not created yet")

        jwt_token = _np_payout_jwt()

        verify_url = f'https://api.nowpayments.io/v1/payout/{wd.batch_withdrawal_id}/verify'
        payload = {"verification_code": verification_code}

        r = requests.post(
            verify_url,
            json=payload,
            headers=_np_headers(jwt_token),
            timeout=30,
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"NOWPayments verify payout error: {r.text}")

        data = r.json()
        wd.payout_status = "verified"
        wd.payout_response_json = json.dumps(data)
        wd.status = "sent"
        db.commit()

        return {
            "ok": True,
            "withdrawal_id": wd.id,
            "status": wd.status,
            "payout_status": wd.payout_status,
            "np": data,
        }
    finally:
        db.close()

@app.get("/admin/withdrawals/{withdrawal_id}/payout-status")
def admin_withdrawal_payout_status(
    withdrawal_id: int,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if not wd.batch_withdrawal_id:
            raise HTTPException(status_code=400, detail="Payout batch not created yet")

        jwt_token = _np_payout_jwt()
        status_url = f'https://api.nowpayments.io/v1/payout/{wd.batch_withdrawal_id}'

        r = requests.get(status_url, headers=_np_headers(jwt_token), timeout=30)
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"NOWPayments payout status error: {r.text}")

        data = r.json()
        ext_status = (
            data.get("status")
            or data.get("result", {}).get("status")
            or (data.get("withdrawals") or [{}])[0].get("status")
            or ""
        )
        ext_status = str(ext_status).lower().strip()

        wd.payout_status = ext_status or wd.payout_status
        wd.payout_response_json = json.dumps(data)

        # completar en el sistema solo cuando el proveedor ya terminó
        if ext_status in {"finished", "completed", "success", "sent"} and wd.status != "completed":
            w = db.query(Wallet).filter(Wallet.user_id == wd.user_id).with_for_update().one()

            amt = float(wd.amount_usd or 0)

            w.balance_pending = _round2(float(w.balance_pending or 0) - amt)
            w.balance_total = _round2(float(w.balance_total or 0) - amt)

            tx = Transaction(
                user_id=wd.user_id,
                type="withdrawal_complete",
                amount=-amt,
                balance_after=float(w.balance_total),
                reference=f"withdrawal:{wd.id}",
            )
            db.add(tx)
            wd.status = "completed"

        db.commit()

        return {
            "ok": True,
            "withdrawal_id": wd.id,
            "status": wd.status,
            "payout_status": wd.payout_status,
            "np": data,
        }
    finally:
        db.close()




@app.post("/admin/users/{user_id}/kyc-tier")
async def admin_set_user_kyc_tier(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)
    body = await request.json()
    kyc_level = int(body.get("kyc_level", 0))
    auto_withdraw_enabled = bool(body.get("auto_withdraw_enabled", True))
    auto_withdraw_limit = body.get("auto_withdraw_limit", None)
    manual_only = bool(body.get("manual_only", False))

    if kyc_level < 0:
        raise HTTPException(status_code=400, detail="kyc_level must be >= 0")

    if auto_withdraw_limit is None:
        if kyc_level >= 2:
            auto_withdraw_limit = 1000.0
        elif kyc_level == 1:
            auto_withdraw_limit = 200.0
        else:
            auto_withdraw_limit = 0.0
    else:
        auto_withdraw_limit = float(auto_withdraw_limit)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if manual_only:
            user.auto_withdraw_enabled = False
            user.auto_withdraw_limit = 0.0
        else:
            user.kyc_level = kyc_level
            user.auto_withdraw_enabled = True if auto_withdraw_enabled else False
            user.auto_withdraw_limit = auto_withdraw_limit

            # simple status mapping for current system
            if kyc_level >= 1:
                user.kyc_status = "verified"
            else:
                user.kyc_status = "unverified"

        db.commit()

        return {
            "ok": True,
            "user_id": user.id,
            "kyc_level": int(user.kyc_level or 0),
            "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
            "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
            "kyc_status": user.kyc_status,
        }
    finally:
        db.close()



def _kyc_user_id_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    token = get_bearer_token(auth)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    payload = decode_token(token)
    user_id = str(payload.get("user_id") or payload.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id

def _save_kyc_file(user_id: str, label: str, upload: UploadFile) -> str:
    safe_user = "".join(c for c in str(user_id) if c.isalnum() or c in ("_","-"))
    safe_name = "".join(c for c in str(upload.filename or "file") if c.isalnum() or c in ("_","-","."))
    if not safe_name:
        safe_name = "file.bin"
    out_dir = f"/var/www/coin2win/uploads/kyc/{safe_user}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/{label}_{int(time.time())}_{safe_name}"
    data = upload.file.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path

@app.post("/api/kyc/upload-level1")
async def api_kyc_upload_level1(request: Request, id_document: UploadFile = File(...), selfie: UploadFile = File(...)):
    user_id = _kyc_user_id_from_request(request)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.kyc_doc_front_path = _save_kyc_file(user_id, "id_document", id_document)
        user.kyc_selfie_path = _save_kyc_file(user_id, "selfie", selfie)
        user.kyc_status = "pending"
        user.kyc_rejected_reason = None
        db.commit()
        with engine.begin() as conn:
            _insert_kyc_audit_log(conn, user_id, "upload_level1", actor=user_id, from_level=int(user.kyc_level or 0), to_level=int(user.kyc_level or 0), note="ID + selfie uploaded")
        return {"ok": True, "user_id": user_id, "kyc_status": user.kyc_status}
    finally:
        db.close()

@app.post("/api/kyc/upload-level2")
async def api_kyc_upload_level2(request: Request, proof_of_address: UploadFile = File(...)):
    user_id = _kyc_user_id_from_request(request)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.kyc_poa_path = _save_kyc_file(user_id, "proof_of_address", proof_of_address)
        if user.kyc_status == "unverified":
            user.kyc_status = "pending"
        db.commit()
        with engine.begin() as conn:
            _insert_kyc_audit_log(conn, user_id, "upload_level2", actor=user_id, from_level=int(user.kyc_level or 0), to_level=int(user.kyc_level or 0), note="Proof of address uploaded")
        return {"ok": True, "user_id": user_id, "kyc_status": user.kyc_status}
    finally:
        db.close()

@app.get("/admin/users/{user_id}/kyc-files")
def admin_user_kyc_files(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "ok": True,
            "user_id": user.id,
            "kyc_status": user.kyc_status,
            "files": {
                "id_document": f"/uploads/kyc/{user.id}/" + os.path.basename(user.kyc_doc_front_path) if user.kyc_doc_front_path else None,
                "selfie": f"/uploads/kyc/{user.id}/" + os.path.basename(user.kyc_selfie_path) if user.kyc_selfie_path else None,
                "proof_of_address": f"/uploads/kyc/{user.id}/" + os.path.basename(user.kyc_poa_path) if user.kyc_poa_path else None,
            }
        }
    finally:
        db.close()



@app.post("/admin/users/{user_id}/kyc-approve")
async def admin_kyc_approve(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)
    body = await request.json()
    kyc_level = int(body.get("kyc_level", 1))

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        prev_level = int(user.kyc_level or 0)
        user.kyc_status = "verified"
        user.kyc_level = kyc_level
        user.kyc_rejected_reason = None
        user.kyc_verified_at = datetime.now(timezone.utc)
        user.kyc_approved_by = "admin"

        if kyc_level >= 2:
            user.auto_withdraw_limit = 1000.0
            user.auto_withdraw_enabled = True
        elif kyc_level == 1:
            user.auto_withdraw_limit = 200.0
            user.auto_withdraw_enabled = True
        else:
            user.auto_withdraw_limit = 0.0
            user.auto_withdraw_enabled = False

        db.commit()
        return {
            "ok": True,
            "user_id": user.id,
            "kyc_status": user.kyc_status,
            "kyc_level": int(user.kyc_level or 0),
            "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
            "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
        }
    finally:
        db.close()


@app.post("/admin/users/{user_id}/kyc-reject")
async def admin_kyc_reject(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)
    body = await request.json()
    reason = str(body.get("reason", "KYC rejected")).strip() or "KYC rejected"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        prev_level = int(user.kyc_level or 0)
        user.kyc_status = "rejected"
        user.kyc_level = 0
        user.auto_withdraw_enabled = False
        user.auto_withdraw_limit = 0.0
        user.kyc_rejected_reason = reason

        db.commit()
        with engine.begin() as conn:
            _insert_kyc_audit_log(conn, user.id, "reject_kyc", actor="admin", from_level=prev_level, to_level=0, note=reason)
        return {
            "ok": True,
            "user_id": user.id,
            "kyc_status": user.kyc_status,
            "kyc_level": int(user.kyc_level or 0),
            "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
            "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
            "kyc_rejected_reason": user.kyc_rejected_reason,
        }
    finally:
        db.close()


@app.get("/api/kyc/me")
def api_kyc_me(request: Request):
    user_id = _kyc_user_id_from_request(request)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "ok": True,
            "user_id": user.id,
            "kyc_status": user.kyc_status,
            "kyc_level": int(user.kyc_level or 0),
            "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
            "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
            "has_id_document": bool(user.kyc_doc_front_path),
            "has_selfie": bool(user.kyc_selfie_path),
            "has_proof_of_address": bool(user.kyc_poa_path),
            "kyc_rejected_reason": user.kyc_rejected_reason,
        }
    finally:
        db.close()



@app.post("/admin/users/{user_id}/kyc-set-manual")
async def admin_kyc_set_manual(
    user_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        prev_level = int(user.kyc_level or 0)
        # preserve KYC status/level; only force withdrawals to manual
        user.auto_withdraw_enabled = False
        user.auto_withdraw_limit = 0.0

        db.commit()
        with engine.begin() as conn:
            _insert_kyc_audit_log(conn, user.id, "set_manual", actor="admin", from_level=prev_level, to_level=prev_level, note="Manual withdrawals only")
        return {
            "ok": True,
            "user_id": user.id,
            "kyc_status": user.kyc_status,
            "kyc_level": int(user.kyc_level or 0),
            "auto_withdraw_enabled": bool(user.auto_withdraw_enabled),
            "auto_withdraw_limit": float(user.auto_withdraw_limit or 0),
        }
    finally:
        db.close()


def _insert_kyc_audit_log(conn, user_id: str, action: str, actor: str | None = None, from_level: int | None = None, to_level: int | None = None, note: str | None = None):
    conn.execute(text("""
        INSERT INTO kyc_audit_logs (user_id, action, actor, from_level, to_level, note)
        VALUES (:user_id, :action, :actor, :from_level, :to_level, :note)
    """), {
        "user_id": user_id,
        "action": action,
        "actor": actor,
        "from_level": from_level,
        "to_level": to_level,
        "note": note,
    })


@app.get("/admin/users/{user_id}/kyc-detail")
def admin_user_kyc_detail(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    conn = engine.connect()
    try:
        user = conn.execute(text("""
            SELECT
                u.id,
                u.kyc_status,
                u.kyc_level,
                u.auto_withdraw_enabled,
                u.auto_withdraw_limit,
                u.kyc_verified_at,
                u.kyc_rejected_reason,
                u.kyc_approved_by,
                u.kyc_doc_front_path,
                u.kyc_selfie_path,
                u.kyc_poa_path
            FROM users u
            WHERE u.id = :id
        """), {"id": user_id}).mappings().first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        logs = conn.execute(text("""
            SELECT id, user_id, action, actor, from_level, to_level, note, created_at
            FROM kyc_audit_logs
            WHERE user_id = :id
            ORDER BY created_at DESC, id DESC
            LIMIT 50
        """), {"id": user_id}).mappings().all()

        return {
            "ok": True,
            "user_id": user["id"],
            "kyc_status": user["kyc_status"],
            "kyc_level": int(user["kyc_level"] or 0),
            "auto_withdraw_enabled": bool(user["auto_withdraw_enabled"]),
            "auto_withdraw_limit": float(user["auto_withdraw_limit"] or 0),
            "kyc_verified_at": user["kyc_verified_at"].isoformat() if user["kyc_verified_at"] else None,
            "kyc_rejected_reason": user["kyc_rejected_reason"],
            "kyc_approved_by": user["kyc_approved_by"],
            "files": {
                "id_document": f"/uploads/kyc/{user_id}/" + os.path.basename(user["kyc_doc_front_path"]) if user["kyc_doc_front_path"] else None,
                "selfie": f"/uploads/kyc/{user_id}/" + os.path.basename(user["kyc_selfie_path"]) if user["kyc_selfie_path"] else None,
                "proof_of_address": f"/uploads/kyc/{user_id}/" + os.path.basename(user["kyc_poa_path"]) if user["kyc_poa_path"] else None,
            },
            "history": [
                {
                    "id": int(r["id"]),
                    "action": r["action"],
                    "actor": r["actor"],
                    "from_level": r["from_level"],
                    "to_level": r["to_level"],
                    "note": r["note"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in logs
            ]
        }
    finally:
        conn.close()
