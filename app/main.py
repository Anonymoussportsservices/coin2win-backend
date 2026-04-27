import uuid
import os
import hmac
import hashlib
import json
import time
import requests
import random

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

from app.agent_auth import router as agent_auth_router

load_dotenv("/var/www/coin2win/.env")

from app.auth_routes import get_bearer_token, decode_token
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
    Boolean,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError


def _guard_user_rate_limit(user, seconds: float = 0.5):
    import time
    now = time.time()
    last = getattr(user, "last_bet_ts", 0)
    if now - last < seconds:
        raise HTTPException(status_code=429, detail="Too fast")
    user.last_bet_ts = now


def _guard_user_lock(user):
    if getattr(user, "bet_lock", False):
        raise HTTPException(status_code=429, detail="Bet in progress")
    user.bet_lock = True

def _release_user_lock(user):
    user.bet_lock = False

app = FastAPI()
app.include_router(agent_auth_router)
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

# --------------------------
# SoftSwiss Config / Helpers
# --------------------------
SOFTSWISS_ENABLED = os.getenv("SOFTSWISS_ENABLED", "false").lower() == "true"
SOFTSWISS_BASE_URL = os.getenv("SOFTSWISS_BASE_URL", "").strip().rstrip("/")
SOFTSWISS_CASINO_ID = os.getenv("SOFTSWISS_CASINO_ID", "").strip()
SOFTSWISS_AUTH_TOKEN = os.getenv("SOFTSWISS_AUTH_TOKEN", "").strip()
SOFTSWISS_DEFAULT_CURRENCY = os.getenv("SOFTSWISS_DEFAULT_CURRENCY", "USD").strip()
SOFTSWISS_DEFAULT_LOCALE = os.getenv("SOFTSWISS_DEFAULT_LOCALE", "en").strip()
SOFTSWISS_DEFAULT_JURISDICTION = os.getenv("SOFTSWISS_DEFAULT_JURISDICTION", "CR").strip()
SOFTSWISS_RETURN_URL = os.getenv("SOFTSWISS_RETURN_URL", "https://coin2win.bet/casino").strip()
SOFTSWISS_DEPOSIT_URL = os.getenv("SOFTSWISS_DEPOSIT_URL", "https://coin2win.bet/cashier").strip()

def _softswiss_compact_json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _softswiss_sign_body(raw_body: bytes) -> str:
    if not SOFTSWISS_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="SoftSwiss auth token missing")
    return hmac.new(
        SOFTSWISS_AUTH_TOKEN.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

def _softswiss_verify_signature(raw_body: bytes, signature: str | None):
    expected = _softswiss_sign_body(raw_body)
    provided = str(signature or "").strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=400, detail="Invalid SoftSwiss signature")

def _softswiss_money(value) -> float:
    try:
        return _round2(float(value or 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")


def _validate_softswiss_session(db, user_id: str, session_payload: str):
    session_payload = str(session_payload or "").strip()
    user_id = str(user_id or "").strip()

    # Local/mock tests used empty session_payload before real launcher.
    # In production mode, require a real session payload.
    if not session_payload:
        if SOFTSWISS_ENABLED:
            raise HTTPException(status_code=400, detail="session_payload required")
        return None

    sess = db.execute(text("""
        SELECT id, user_id, game_id, status
        FROM softswiss_sessions
        WHERE session_payload = :session_payload
        LIMIT 1
    """), {"session_payload": session_payload}).mappings().first()

    if not sess:
        raise HTTPException(status_code=400, detail="Invalid session_payload")

    if str(sess["user_id"]) != str(user_id):
        raise HTTPException(status_code=400, detail="session_payload user mismatch")

    if str(sess["status"] or "").lower() != "active":
        raise HTTPException(status_code=400, detail="session is not active")

    return sess


if not NOWPAYMENTS_API_KEY:
    raise RuntimeError("Missing NOWPAYMENTS_API_KEY in .env")
if not NOWPAYMENTS_IPN_SECRET:
    raise RuntimeError("Missing NOWPAYMENTS_IPN_SECRET in .env")
if not IPN_CALLBACK_URL:
    raise RuntimeError("Missing NOWPAYMENTS_IPN_CALLBACK_URL in .env")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in .env")

ACTIVE_WITHDRAW_STATUSES = ("requested", "approved", "sent")

KYC_LEVEL_LIMITS = {
    1: 200.0,
    2: 1000.0,
}

WITHDRAW_BLOCK_REASONS = {
    "USER_ID_REQUIRED": "user_id required",
    "INVALID_AMOUNT": "amount_usd must be > 0",
    "MIN_WITHDRAW_NOT_MET": f"Minimum withdrawal is ${MIN_WITHDRAW_USD:.0f}",
    "PAYOUT_CURRENCY_REQUIRED": "payout_currency required",
    "PAYOUT_ADDRESS_REQUIRED": "payout_address required",
    "KYC_REQUIRED": "KYC verification required before withdrawals are enabled.",
    "WITHDRAWAL_ALREADY_ACTIVE": "You already have a withdrawal in progress. Please wait until it is completed or rejected.",
    "KYC_LEVEL_TOO_LOW": "Your KYC level does not allow withdrawals yet.",
    "AUTO_WITHDRAW_DISABLED": "Withdrawals are not enabled for this account yet.",
    "AUTO_WITHDRAW_LIMIT_EXCEEDED": "Withdrawal amount exceeds your current approved limit.",
    "INSUFFICIENT_AVAILABLE_BALANCE": "Insufficient balance",
    "PROCESSOR_REF_REQUIRED": "processor_ref required",
}

def _withdraw_error(reason_code: str, status_code: int):
    return HTTPException(
        status_code=status_code,
        detail={
            "reason_code": reason_code,
            "message": WITHDRAW_BLOCK_REASONS[reason_code],
        },
    )

# --------------------------
# Database
# --------------------------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String(64), primary_key=True)
    kyc_status = Column(String(20), nullable=False, default="unverified", server_default="unverified")
    kyc_level = Column(Integer, nullable=False, default=0, server_default="0")
    auto_withdraw_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    auto_withdraw_limit = Column(Float, nullable=False, default=0, server_default="0")
    kyc_submitted_at = Column(DateTime(timezone=True), nullable=True)
    kyc_verified_at = Column(DateTime(timezone=True), nullable=True)
    kyc_rejected_at = Column(DateTime(timezone=True), nullable=True)
    kyc_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    kyc_approved_by = Column(Text, nullable=True)
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

    approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(String(128), nullable=True)
    processor_ref = Column(String(255), nullable=True)
    failure_reason = Column(Text, nullable=True)

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

class WithdrawalAuditLog(Base):
    __tablename__ = "withdrawal_audit_logs"
    id = Column(Integer, primary_key=True)
    withdrawal_id = Column(Integer, nullable=False)
    action = Column(String(64), nullable=False)
    actor_id = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

@app.on_event("startup")
def startup():
    pass
    



# ==========================
# STUDIO: COINFLIP
# ==========================

class CoinflipBet(Base):
    __tablename__ = "coinflip_bets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    amount_usd = Column(Float, nullable=False)
    choice = Column(String(8), nullable=False)
    result = Column(String(8), nullable=False)
    win = Column(Boolean, nullable=False, default=False)
    payout = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class HiloBet(Base):
    __tablename__ = "hilo_bets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    amount_usd = Column(Float, nullable=False)
    start_card = Column(Integer, nullable=False)
    current_card = Column(Integer, nullable=False)
    result_card = Column(Integer, nullable=True)
    choice = Column(String(8), nullable=True)
    streak = Column(Integer, nullable=False, default=0)
    multiplier = Column(Float, nullable=False, default=1.0)
    status = Column(String(32), nullable=False, default="active")  # active, lost, cashed_out
    payout = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


@app.post("/studio/coinflip/bet")
async def coinflip_bet(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id","")).strip()
    amount = float(body.get("amount_usd",0))
    choice = str(body.get("choice","")).lower().strip()

    if not user_id: raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0: raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    if choice not in ("heads","tails"): raise HTTPException(status_code=400, detail="choice must be heads or tails")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if float(w.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        w.balance_total = _round2(w.balance_total - amount)
        w.balance_available = _round2(w.balance_available - amount)

        tx1 = Transaction(user_id=user_id,type="coinflip_bet",amount=-float(amount),balance_after=float(w.balance_total),reference=None)
        db.add(tx1)

        import random
        result = "heads" if random.random() < 0.5 else "tails"
        win = result == choice

        payout = 0.0
        if win:
            payout = _round2(amount * 1.98)
            w.balance_total = _round2(w.balance_total + payout)
            w.balance_available = _round2(w.balance_available + payout)
            tx2 = Transaction(user_id=user_id,type="coinflip_payout",amount=float(payout),balance_after=float(w.balance_total),reference=None)
            db.add(tx2)

        bet = CoinflipBet(user_id=user_id,amount_usd=float(amount),choice=choice,result=result,win=bool(win),payout=float(payout))
        db.add(bet); db.flush()

        tx1.reference = f"coinflip_bet:{bet.id}"
        if win: tx2.reference = f"coinflip_bet:{bet.id}"

        db.commit()

        return {"ok":True,"bet_id":bet.id,"user_id":user_id,"amount_usd":float(amount),"choice":choice,"result":result,"win":bool(win),"payout":float(payout),"wallet":_serialize_wallet(w)}
    finally:
        db.close()




# --------------------------
# SoftSwiss callbacks
# --------------------------
@app.post("/v2/a8r_casino.Player/Balance")
async def softswiss_player_balance(request: Request, x_request_sign: str | None = Header(default=None, alias="X-REQUEST-SIGN")):
    raw_body = await request.body()
    _softswiss_verify_signature(raw_body, x_request_sign)

    body = json.loads(raw_body.decode("utf-8") or "{}")
    user_id = str(body.get("player_id") or body.get("user_id") or "").strip()
    session_payload = str(body.get("session_payload") or "").strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="player_id required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).one()

        _validate_softswiss_session(db, user_id, session_payload)

        return {
            "balance": _round2(float(wallet.balance_available or 0)),
            "currency": SOFTSWISS_DEFAULT_CURRENCY,
        }
    finally:
        db.close()




@app.post("/v2/a8r_casino.Round/BetWin")
async def softswiss_round_betwin(request: Request, x_request_sign: str | None = Header(default=None, alias="X-REQUEST-SIGN")):
    raw_body = await request.body()
    _softswiss_verify_signature(raw_body, x_request_sign)

    body = json.loads(raw_body.decode("utf-8") or "{}")
    user_id = str(body.get("player_id") or body.get("user_id") or "").strip()
    session_payload = str(body.get("session_payload") or "").strip()
    round_id = str(body.get("round_id") or body.get("round") or "").strip()
    game_id = str(body.get("game_id") or body.get("game") or "").strip()
    currency = str(body.get("currency") or SOFTSWISS_DEFAULT_CURRENCY).strip()

    txs = body.get("transactions") or body.get("txs") or []
    if isinstance(txs, dict):
        txs = [txs]

    if not user_id:
        raise HTTPException(status_code=400, detail="player_id required")
    if not isinstance(txs, list) or not txs:
        raise HTTPException(status_code=400, detail="transactions required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        _validate_softswiss_session(db, user_id, session_payload)
        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if round_id:
            db.execute(text("""
                INSERT INTO softswiss_rounds (provider, round_id, user_id, game_id, session_payload, status)
                VALUES ('softswiss', :round_id, :user_id, :game_id, :session_payload, 'open')
                ON CONFLICT (provider, round_id) DO NOTHING
            """), {
                "round_id": round_id,
                "user_id": user_id,
                "game_id": game_id,
                "session_payload": session_payload,
            })

        responses = []

        for tx in txs:
            provider_tx_id = str(tx.get("id") or tx.get("transaction_id") or tx.get("tx_id") or "").strip()
            tx_type = str(tx.get("type") or tx.get("action") or "").lower().strip()
            amount = _softswiss_money(tx.get("amount"))

            if not provider_tx_id:
                raise HTTPException(status_code=400, detail="transaction id required")
            if tx_type not in ("bet", "win"):
                raise HTTPException(status_code=400, detail=f"unsupported transaction type: {tx_type}")

            existing = db.execute(text("""
                SELECT id, status, wallet_delta
                FROM softswiss_transactions
                WHERE provider_transaction_id = :provider_tx_id
                LIMIT 1
            """), {"provider_tx_id": provider_tx_id}).fetchone()

            if existing:
                responses.append({
                    "id": provider_tx_id,
                    "status": "duplicate",
                    "balance": _round2(float(wallet.balance_available or 0)),
                })
                continue

            tombstone = db.execute(text("""
                SELECT id
                FROM softswiss_transactions
                WHERE original_transaction_id = :provider_tx_id
                  AND status = 'tombstone'
                LIMIT 1
            """), {"provider_tx_id": provider_tx_id}).fetchone()

            if tombstone:
                responses.append({
                    "id": provider_tx_id,
                    "status": "cancelled_by_rollback",
                    "balance": _round2(float(wallet.balance_available or 0)),
                })
                continue

            if tx_type == "bet":
                if float(wallet.balance_available or 0) < amount:
                    raise HTTPException(status_code=400, detail="Insufficient balance")

                wallet_delta = -amount
                wallet.balance_total = _round2(float(wallet.balance_total or 0) + wallet_delta)
                wallet.balance_available = _round2(float(wallet.balance_available or 0) + wallet_delta)
                visible_type = "softswiss_bet"

            else:
                wallet_delta = amount
                wallet.balance_total = _round2(float(wallet.balance_total or 0) + wallet_delta)
                wallet.balance_available = _round2(float(wallet.balance_available or 0) + wallet_delta)
                visible_type = "softswiss_win"

            db.execute(text("""
                INSERT INTO softswiss_transactions
                (provider, provider_transaction_id, user_id, round_id, game_id, session_payload, type, amount, wallet_delta, currency, status, raw_payload)
                VALUES
                ('softswiss', :provider_tx_id, :user_id, :round_id, :game_id, :session_payload, :tx_type, :amount, :wallet_delta, :currency, 'processed', CAST(:raw_payload AS jsonb))
            """), {
                "provider_tx_id": provider_tx_id,
                "user_id": user_id,
                "round_id": round_id,
                "game_id": game_id,
                "session_payload": session_payload,
                "tx_type": tx_type,
                "amount": amount,
                "wallet_delta": wallet_delta,
                "currency": currency,
                "raw_payload": json.dumps(tx),
            })

            db.add(Transaction(
                user_id=user_id,
                type=visible_type,
                amount=float(wallet_delta),
                balance_after=float(wallet.balance_total),
                reference=f"softswiss:{provider_tx_id}",
            ))

            responses.append({
                "id": provider_tx_id,
                "status": "processed",
                "balance": _round2(float(wallet.balance_available or 0)),
            })

        db.commit()

        return {
            "balance": _round2(float(wallet.balance_available or 0)),
            "currency": currency,
            "transactions": responses,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SoftSwiss BetWin failed: {str(e)}")
    finally:
        db.close()




@app.post("/v2/a8r_casino.Round/Rollback")
async def softswiss_round_rollback(request: Request, x_request_sign: str | None = Header(default=None, alias="X-REQUEST-SIGN")):
    raw_body = await request.body()
    _softswiss_verify_signature(raw_body, x_request_sign)

    body = json.loads(raw_body.decode("utf-8") or "{}")
    user_id = str(body.get("player_id") or body.get("user_id") or "").strip()
    session_payload = str(body.get("session_payload") or "").strip()
    round_id = str(body.get("round_id") or body.get("round") or "").strip()
    game_id = str(body.get("game_id") or body.get("game") or "").strip()
    currency = str(body.get("currency") or SOFTSWISS_DEFAULT_CURRENCY).strip()

    rollbacks = body.get("transactions") or body.get("rollbacks") or body.get("txs") or []
    if isinstance(rollbacks, dict):
        rollbacks = [rollbacks]

    if not user_id:
        raise HTTPException(status_code=400, detail="player_id required")
    if not isinstance(rollbacks, list) or not rollbacks:
        raise HTTPException(status_code=400, detail="rollback transactions required")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        _validate_softswiss_session(db, user_id, session_payload)
        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        responses = []

        for rb in rollbacks:
            rollback_tx_id = str(rb.get("id") or rb.get("rollback_transaction_id") or rb.get("transaction_id") or "").strip()
            original_tx_id = str(rb.get("original_id") or rb.get("original_transaction_id") or rb.get("original_tx_id") or "").strip()

            if not rollback_tx_id:
                raise HTTPException(status_code=400, detail="rollback transaction id required")
            if not original_tx_id:
                raise HTTPException(status_code=400, detail="original transaction id required")

            existing_rb = db.execute(text("""
                SELECT id, status
                FROM softswiss_transactions
                WHERE rollback_transaction_id = :rollback_tx_id
                LIMIT 1
            """), {"rollback_tx_id": rollback_tx_id}).fetchone()

            if existing_rb:
                responses.append({
                    "id": rollback_tx_id,
                    "original_id": original_tx_id,
                    "status": "duplicate",
                    "balance": _round2(float(wallet.balance_available or 0)),
                })
                continue

            original = db.execute(text("""
                SELECT id, provider_transaction_id, type, amount, wallet_delta, status
                FROM softswiss_transactions
                WHERE provider_transaction_id = :original_tx_id
                LIMIT 1
            """), {"original_tx_id": original_tx_id}).fetchone()

            if not original:
                db.execute(text("""
                    INSERT INTO softswiss_transactions
                    (provider, rollback_transaction_id, original_transaction_id, user_id, round_id, game_id, session_payload, type, amount, wallet_delta, currency, status, raw_payload)
                    VALUES
                    ('softswiss', :rollback_tx_id, :original_tx_id, :user_id, :round_id, :game_id, :session_payload, 'rollback', 0, 0, :currency, 'tombstone', CAST(:raw_payload AS jsonb))
                """), {
                    "rollback_tx_id": rollback_tx_id,
                    "original_tx_id": original_tx_id,
                    "user_id": user_id,
                    "round_id": round_id,
                    "game_id": game_id,
                    "session_payload": session_payload,
                    "currency": currency,
                    "raw_payload": json.dumps(rb),
                })
                responses.append({
                    "id": rollback_tx_id,
                    "original_id": original_tx_id,
                    "status": "tombstone",
                    "balance": _round2(float(wallet.balance_available or 0)),
                })
                continue

            original_status = str(original.status or "")
            if original_status == "rolled_back":
                responses.append({
                    "id": rollback_tx_id,
                    "original_id": original_tx_id,
                    "status": "already_rolled_back",
                    "balance": _round2(float(wallet.balance_available or 0)),
                })
                continue

            original_delta = float(original.wallet_delta or 0)
            rollback_delta = _round2(-original_delta)

            # Reversal:
            # original bet delta was negative, rollback credits money back.
            # original win delta was positive, rollback debits money back.
            if rollback_delta < 0 and float(wallet.balance_available or 0) < abs(rollback_delta):
                raise HTTPException(status_code=400, detail="Insufficient balance for rollback")

            wallet.balance_total = _round2(float(wallet.balance_total or 0) + rollback_delta)
            wallet.balance_available = _round2(float(wallet.balance_available or 0) + rollback_delta)

            db.execute(text("""
                UPDATE softswiss_transactions
                SET status = 'rolled_back'
                WHERE provider_transaction_id = :original_tx_id
            """), {"original_tx_id": original_tx_id})

            db.execute(text("""
                INSERT INTO softswiss_transactions
                (provider, rollback_transaction_id, original_transaction_id, user_id, round_id, game_id, session_payload, type, amount, wallet_delta, currency, status, raw_payload)
                VALUES
                ('softswiss', :rollback_tx_id, :original_tx_id, :user_id, :round_id, :game_id, :session_payload, 'rollback', :amount, :wallet_delta, :currency, 'processed', CAST(:raw_payload AS jsonb))
            """), {
                "rollback_tx_id": rollback_tx_id,
                "original_tx_id": original_tx_id,
                "user_id": user_id,
                "round_id": round_id,
                "game_id": game_id,
                "session_payload": session_payload,
                "amount": abs(original_delta),
                "wallet_delta": rollback_delta,
                "currency": currency,
                "raw_payload": json.dumps(rb),
            })

            db.add(Transaction(
                user_id=user_id,
                type="softswiss_rollback",
                amount=float(rollback_delta),
                balance_after=float(wallet.balance_total),
                reference=f"softswiss_rollback:{rollback_tx_id}",
            ))

            responses.append({
                "id": rollback_tx_id,
                "original_id": original_tx_id,
                "status": "processed",
                "balance": _round2(float(wallet.balance_available or 0)),
            })

        db.commit()

        return {
            "balance": _round2(float(wallet.balance_available or 0)),
            "currency": currency,
            "transactions": responses,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SoftSwiss Rollback failed: {str(e)}")
    finally:
        db.close()




@app.post("/v2/a8r_casino.Round/Finish")
async def softswiss_round_finish(request: Request, x_request_sign: str | None = Header(default=None, alias="X-REQUEST-SIGN")):
    raw_body = await request.body()
    _softswiss_verify_signature(raw_body, x_request_sign)

    body = json.loads(raw_body.decode("utf-8") or "{}")
    user_id = str(body.get("player_id") or body.get("user_id") or "").strip()
    session_payload = str(body.get("session_payload") or "").strip()
    round_id = str(body.get("round_id") or body.get("round") or "").strip()
    game_id = str(body.get("game_id") or body.get("game") or "").strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="player_id required")
    if not round_id:
        raise HTTPException(status_code=400, detail="round_id required")

    db = SessionLocal()
    try:
        _validate_softswiss_session(db, user_id, session_payload)
        db.execute(text("""
            INSERT INTO softswiss_rounds (provider, round_id, user_id, game_id, session_payload, status)
            VALUES ('softswiss', :round_id, :user_id, :game_id, :session_payload, 'closed')
            ON CONFLICT (provider, round_id)
            DO UPDATE SET status='closed', updated_at=NOW()
        """), {
            "round_id": round_id,
            "user_id": user_id,
            "game_id": game_id,
            "session_payload": session_payload,
        })
        db.commit()
        return {"ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SoftSwiss Finish failed: {str(e)}")
    finally:
        db.close()


@app.get("/studio/coinflip/bets/{user_id}")
def coinflip_bets(user_id: str, limit: int = 50):
    db = SessionLocal()
    try:
        rows = db.query(CoinflipBet).filter(CoinflipBet.user_id == user_id).order_by(CoinflipBet.id.desc()).limit(max(1,min(limit,200))).all()
        return {"user_id":user_id,"count":len(rows),"bets":[{"id":b.id,"amount_usd":float(b.amount_usd),"choice":b.choice,"result":b.result,"win":bool(b.win),"payout":float(b.payout),"created_at":str(b.created_at)} for b in rows]}
    finally:
        db.close()


# ==========================
# STUDIO: HI-LO
# ==========================
HILO_HOUSE_EDGE = float(os.getenv("HILO_HOUSE_EDGE", "0.05"))

def _hilo_draw_card() -> int:
    return random.randint(1, 13)

def _hilo_allowed_choices(card: int) -> list[str]:
    if int(card) <= 1:
        return ["high"]
    if int(card) >= 13:
        return ["low"]
    return ["high", "low"]

def _hilo_win_probability(card: int, choice: str) -> float:
    c = int(card)
    ch = str(choice or "").lower().strip()
    if ch == "high":
        wins = max(0, 13 - c)
    elif ch == "low":
        wins = max(0, c - 1)
    else:
        return 0.0
    return wins / 13.0

def _hilo_next_multiplier(current_multiplier: float, card: int, choice: str) -> float:
    wp = _hilo_win_probability(card, choice)
    if wp <= 0:
        raise HTTPException(status_code=400, detail="Invalid HI-LO probability")
    return _round2(float(current_multiplier) * ((1.0 - float(HILO_HOUSE_EDGE)) / wp))

@app.post("/studio/hilo/start")
async def hilo_start(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if float(w.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        active = db.query(HiloBet).filter(HiloBet.user_id == user_id, HiloBet.status == "active").order_by(HiloBet.id.desc()).one_or_none()
        if active:
            raise HTTPException(status_code=400, detail="Active HI-LO round already exists")

        w.balance_total = _round2(w.balance_total - amount)
        w.balance_available = _round2(w.balance_available - amount)

        tx1 = Transaction(user_id=user_id, type="hilo_bet", amount=-float(amount), balance_after=float(w.balance_total), reference=None)
        db.add(tx1)

        card = _hilo_draw_card()
        bet = HiloBet(user_id=user_id, amount_usd=float(amount), start_card=int(card), current_card=int(card), result_card=None, choice=None, streak=0, multiplier=1.0, status="active", payout=0.0)
        db.add(bet)
        db.flush()

        tx1.reference = f"hilo_bet:{bet.id}"
        db.commit()

        return {"ok": True, "bet_id": bet.id, "user_id": user_id, "amount_usd": float(amount), "start_card": int(bet.start_card), "current_card": int(bet.current_card), "streak": int(bet.streak), "multiplier": float(bet.multiplier), "status": str(bet.status), "allowed_choices": _hilo_allowed_choices(int(bet.current_card)), "cashout_value": 0.0, "wallet": _serialize_wallet(w)}
    finally:
        db.close()

@app.post("/studio/hilo/next")
async def hilo_next(request: Request):
    body = await request.json()
    bet_id = int(body.get("bet_id", 0))
    choice = str(body.get("choice", "")).lower().strip()

    if bet_id <= 0:
        raise HTTPException(status_code=400, detail="bet_id required")
    if choice not in ("high", "low"):
        raise HTTPException(status_code=400, detail="choice must be high or low")

    db = SessionLocal()
    try:
        bet = db.query(HiloBet).filter(HiloBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="HI-LO bet not found")
        if str(bet.status) != "active":
            raise HTTPException(status_code=400, detail="HI-LO round is not active")

        current_card = int(bet.current_card)
        allowed = _hilo_allowed_choices(current_card)
        if choice not in allowed:
            raise HTTPException(status_code=400, detail=f"Only {allowed[0]} allowed from card {current_card}")

        next_card = _hilo_draw_card()
        won = (choice == "high" and next_card > current_card) or (choice == "low" and next_card < current_card)

        bet.choice = choice
        bet.result_card = int(next_card)

        if won:
            bet.streak = int(bet.streak) + 1
            bet.multiplier = float(_hilo_next_multiplier(float(bet.multiplier), current_card, choice))
            bet.current_card = int(next_card)
            db.commit()
            return {"ok": True, "bet_id": bet.id, "choice": choice, "previous_card": current_card, "result_card": int(next_card), "win": True, "status": str(bet.status), "streak": int(bet.streak), "multiplier": float(bet.multiplier), "cashout_value": _round2(float(bet.amount_usd) * float(bet.multiplier)), "allowed_choices": _hilo_allowed_choices(int(bet.current_card))}

        bet.status = "lost"
        bet.payout = 0.0
        db.commit()
        return {"ok": True, "bet_id": bet.id, "choice": choice, "previous_card": current_card, "result_card": int(next_card), "win": False, "status": str(bet.status), "streak": int(bet.streak), "multiplier": float(bet.multiplier), "cashout_value": 0.0, "allowed_choices": []}
    finally:
        db.close()

@app.post("/studio/hilo/cashout")
async def hilo_cashout(request: Request):
    body = await request.json()
    bet_id = int(body.get("bet_id", 0))
    if bet_id <= 0:
        raise HTTPException(status_code=400, detail="bet_id required")
    db = SessionLocal()
    try:
        bet = db.query(HiloBet).filter(HiloBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="HI-LO bet not found")
        if str(bet.status) != "active":
            raise HTTPException(status_code=400, detail="HI-LO round is not active")
        if int(bet.streak) <= 0:
            raise HTTPException(status_code=400, detail="Cannot cash out before first successful pick")
        w = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()
        payout = _round2(float(bet.amount_usd) * float(bet.multiplier))
        w.balance_total = _round2(w.balance_total + payout)
        w.balance_available = _round2(w.balance_available + payout)
        bet.status = "cashed_out"
        bet.payout = float(payout)
        db.add(Transaction(user_id=bet.user_id, type="hilo_payout", amount=float(payout), balance_after=float(w.balance_total), reference=f"hilo_bet:{bet.id}"))
        db.commit()
        return {"ok": True, "bet_id": bet.id, "status": str(bet.status), "streak": int(bet.streak), "multiplier": float(bet.multiplier), "payout": float(bet.payout), "wallet": _serialize_wallet(w)}
    finally:
        db.close()

@app.get("/studio/hilo/bets/{user_id}")
def hilo_bets(user_id: str, limit: int = 50):
    db = SessionLocal()
    try:
        rows = db.query(HiloBet).filter(HiloBet.user_id == user_id).order_by(HiloBet.id.desc()).limit(max(1, min(limit, 200))).all()
        return {"user_id": user_id, "count": len(rows), "bets": [{"id": b.id, "amount_usd": float(b.amount_usd), "start_card": int(b.start_card), "current_card": int(b.current_card), "result_card": int(b.result_card) if b.result_card is not None else None, "choice": b.choice, "streak": int(b.streak), "multiplier": float(b.multiplier), "status": str(b.status), "payout": float(b.payout), "created_at": str(b.created_at), "updated_at": str(b.updated_at)} for b in rows]}
    finally:
        db.close()


# ==========================
# STUDIO: MINES
# ==========================
MINES_GRID_SIZE = int(os.getenv("MINES_GRID_SIZE", "25"))
MINES_MIN_COUNT = int(os.getenv("MINES_MIN_COUNT", "1"))
MINES_MAX_COUNT = int(os.getenv("MINES_MAX_COUNT", "10"))
MINES_HOUSE_EDGE = float(os.getenv("MINES_HOUSE_EDGE", "0.03"))

class MinesBet(Base):
    __tablename__ = "mines_bets"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    amount_usd = Column(Float, nullable=False)
    mine_count = Column(Integer, nullable=False)
    grid_size = Column(Integer, nullable=False, default=25)
    mines_positions = Column(Text, nullable=False, default="[]")
    revealed_tiles = Column(Text, nullable=False, default="[]")
    hit_mine = Column(Boolean, nullable=False, default=False)
    multiplier = Column(Float, nullable=False, default=1.0)
    status = Column(String(32), nullable=False, default="active")  # active, lost, cashed_out
    payout = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

def _mines_parse_json_list(value) -> list[int]:
    try:
        data = json.loads(value or "[]")
        if not isinstance(data, list):
            return []
        out = []
        for x in data:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    except Exception:
        return []

def _mines_dump_json_list(items: list[int]) -> str:
    return json.dumps([int(x) for x in items])

def _mines_generate_positions(grid_size: int, mine_count: int) -> list[int]:
    if mine_count <= 0 or mine_count >= grid_size:
        raise HTTPException(status_code=400, detail="Invalid mine_count")
    return sorted(random.sample(range(grid_size), mine_count))

def _mines_multiplier(grid_size: int, mine_count: int, safe_reveals: int) -> float:
    safe_tiles = int(grid_size) - int(mine_count)
    r = int(safe_reveals)
    if r <= 0:
        return 1.0
    if r > safe_tiles:
        raise HTTPException(status_code=400, detail="Invalid safe reveals")
    value = (math.comb(int(grid_size), r) / math.comb(int(safe_tiles), r)) * (1.0 - float(MINES_HOUSE_EDGE))
    return _round2(value)

def _serialize_mines_bet(b):
    mines_positions = _mines_parse_json_list(b.mines_positions)
    revealed_tiles = _mines_parse_json_list(b.revealed_tiles)
    safe_reveals = len([x for x in revealed_tiles if x not in set(mines_positions)])
    return {
        "id": int(b.id),
        "amount_usd": float(b.amount_usd),
        "mine_count": int(b.mine_count),
        "grid_size": int(b.grid_size),
        "revealed_tiles": revealed_tiles,
        "revealed_count": len(revealed_tiles),
        "safe_reveals": int(safe_reveals),
        "hit_mine": bool(b.hit_mine),
        "multiplier": float(b.multiplier),
        "status": str(b.status),
        "payout": float(b.payout),
        "created_at": str(b.created_at),
        "updated_at": str(b.updated_at),
    }

@app.post("/studio/mines/start")
async def mines_start(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    amount = float(body.get("amount_usd", 0))
    mine_count = int(body.get("mine_count", 0))

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    if mine_count < MINES_MIN_COUNT or mine_count > MINES_MAX_COUNT:
        raise HTTPException(status_code=400, detail=f"mine_count must be between {MINES_MIN_COUNT} and {MINES_MAX_COUNT}")

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        if float(w.balance_available) < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        active = db.query(MinesBet).filter(MinesBet.user_id == user_id, MinesBet.status == "active").order_by(MinesBet.id.desc()).one_or_none()
        if active:
            raise HTTPException(status_code=400, detail="Active Mines round already exists")

        w.balance_total = _round2(w.balance_total - amount)
        w.balance_available = _round2(w.balance_available - amount)

        tx1 = Transaction(user_id=user_id, type="mines_bet", amount=-float(amount), balance_after=float(w.balance_total), reference=None)
        db.add(tx1)

        mines_positions = _mines_generate_positions(MINES_GRID_SIZE, mine_count)
        bet = MinesBet(
            user_id=user_id,
            amount_usd=float(amount),
            mine_count=int(mine_count),
            grid_size=int(MINES_GRID_SIZE),
            mines_positions=_mines_dump_json_list(mines_positions),
            revealed_tiles="[]",
            hit_mine=False,
            multiplier=1.0,
            status="active",
            payout=0.0,
        )
        db.add(bet)
        db.flush()

        tx1.reference = f"mines_bet:{bet.id}"
        db.commit()

        return {
            "ok": True,
            "bet_id": int(bet.id),
            "user_id": user_id,
            "amount_usd": float(bet.amount_usd),
            "mine_count": int(bet.mine_count),
            "grid_size": int(bet.grid_size),
            "revealed_tiles": [],
            "revealed_count": 0,
            "safe_reveals": 0,
            "multiplier": 1.0,
            "cashout_value": 0.0,
            "status": str(bet.status),
            "wallet": _serialize_wallet(w),
        }
    finally:
        db.close()

@app.post("/studio/mines/reveal")
async def mines_reveal(request: Request):
    body = await request.json()
    bet_id = int(body.get("bet_id", 0))
    tile_index = int(body.get("tile_index", -1))

    if bet_id <= 0:
        raise HTTPException(status_code=400, detail="bet_id required")
    if tile_index < 0 or tile_index >= MINES_GRID_SIZE:
        raise HTTPException(status_code=400, detail=f"tile_index must be between 0 and {MINES_GRID_SIZE - 1}")

    db = SessionLocal()
    try:
        bet = db.query(MinesBet).filter(MinesBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="Mines bet not found")
        if str(bet.status) != "active":
            raise HTTPException(status_code=400, detail="Mines round is not active")

        mines_positions = _mines_parse_json_list(bet.mines_positions)
        revealed_tiles = _mines_parse_json_list(bet.revealed_tiles)

        if tile_index in revealed_tiles:
            raise HTTPException(status_code=400, detail="Tile already revealed")

        revealed_tiles.append(int(tile_index))
        bet.revealed_tiles = _mines_dump_json_list(revealed_tiles)

        if tile_index in set(mines_positions):
            bet.hit_mine = True
            bet.status = "lost"
            bet.payout = 0.0
            db.commit()
            return {
                "ok": True,
                "bet_id": int(bet.id),
                "tile_index": int(tile_index),
                "is_mine": True,
                "revealed_tiles": revealed_tiles,
                "revealed_count": len(revealed_tiles),
                "safe_reveals": len([x for x in revealed_tiles if x not in set(mines_positions)]),
                "multiplier": float(bet.multiplier),
                "cashout_value": 0.0,
                "status": str(bet.status),
                "game_over": True,
            }

        safe_reveals = len([x for x in revealed_tiles if x not in set(mines_positions)])
        bet.multiplier = float(_mines_multiplier(int(bet.grid_size), int(bet.mine_count), int(safe_reveals)))
        db.commit()

        return {
            "ok": True,
            "bet_id": int(bet.id),
            "tile_index": int(tile_index),
            "is_mine": False,
            "revealed_tiles": revealed_tiles,
            "revealed_count": len(revealed_tiles),
            "safe_reveals": int(safe_reveals),
            "multiplier": float(bet.multiplier),
            "cashout_value": _round2(float(bet.amount_usd) * float(bet.multiplier)),
            "status": str(bet.status),
            "game_over": False,
        }
    finally:
        db.close()

@app.post("/studio/mines/cashout")
async def mines_cashout(request: Request):
    body = await request.json()
    bet_id = int(body.get("bet_id", 0))
    if bet_id <= 0:
        raise HTTPException(status_code=400, detail="bet_id required")

    db = SessionLocal()
    try:
        bet = db.query(MinesBet).filter(MinesBet.id == bet_id).with_for_update().one_or_none()
        if not bet:
            raise HTTPException(status_code=404, detail="Mines bet not found")
        if str(bet.status) != "active":
            raise HTTPException(status_code=400, detail="Mines round is not active")

        mines_positions = _mines_parse_json_list(bet.mines_positions)
        revealed_tiles = _mines_parse_json_list(bet.revealed_tiles)
        safe_reveals = len([x for x in revealed_tiles if x not in set(mines_positions)])

        if safe_reveals <= 0:
            raise HTTPException(status_code=400, detail="Cannot cash out before first safe reveal")

        w = db.query(Wallet).filter(Wallet.user_id == bet.user_id).with_for_update().one()
        payout = _round2(float(bet.amount_usd) * float(bet.multiplier))

        w.balance_total = _round2(w.balance_total + payout)
        w.balance_available = _round2(w.balance_available + payout)

        bet.status = "cashed_out"
        bet.payout = float(payout)

        db.add(Transaction(user_id=bet.user_id, type="mines_payout", amount=float(payout), balance_after=float(w.balance_total), reference=f"mines_bet:{bet.id}"))
        db.commit()

        return {
            "ok": True,
            "bet_id": int(bet.id),
            "status": str(bet.status),
            "revealed_count": len(revealed_tiles),
            "safe_reveals": int(safe_reveals),
            "multiplier": float(bet.multiplier),
            "payout": float(bet.payout),
            "wallet": _serialize_wallet(w),
        }
    finally:
        db.close()

@app.get("/studio/mines/bets/{user_id}")
def mines_bets(user_id: str, limit: int = 50):
    db = SessionLocal()
    try:
        rows = db.query(MinesBet).filter(MinesBet.user_id == user_id).order_by(MinesBet.id.desc()).limit(max(1, min(limit, 200))).all()
        return {"user_id": user_id, "count": len(rows), "bets": [_serialize_mines_bet(b) for b in rows]}
    finally:
        db.close()



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
    import sys
    expected = (ADMIN_KEY or "").strip()
    received = (x_admin_key or "").strip()

    print(
        f"[ADMIN_DEBUG] expected_len={len(expected)} received_len={len(received)} "
        f"expected_prefix={expected[:8]!r} received_prefix={received[:8]!r} "
        f"match={received == expected}",
        file=sys.stderr,
        flush=True,
    )

    if not ADMIN_KEY:
        return
    if not x_admin_key or received != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _np_headers():
    return {
        "x-api-key": NOWPAYMENTS_API_KEY,
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



# --------------------------
# Withdrawal Transition Guards
# --------------------------
ALLOWED_TRANSITIONS = {
    "requested": {"approved", "rejected"},
    "approved": {"sent", "rejected"},
    "sent": {"completed"},
    "completed": set(),
    "rejected": set(),
}

def _validate_transition(current: str, target: str):
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current} -> {target}"
        )
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
        "approved_at": str(w.approved_at) if w.approved_at else None,
        "sent_at": str(w.sent_at) if w.sent_at else None,
        "completed_at": str(w.completed_at) if w.completed_at else None,
        "rejected_at": str(w.rejected_at) if w.rejected_at else None,
        "failed_at": str(w.failed_at) if w.failed_at else None,
        "approved_by": w.approved_by,
        "processor_ref": w.processor_ref,
        "failure_reason": w.failure_reason,
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

def _write_withdrawal_audit(db, withdrawal_id: int, action: str, actor_id: str | None = None, note: str | None = None):
    db.add(
        WithdrawalAuditLog(
            withdrawal_id=withdrawal_id,
            action=action,
            actor_id=actor_id,
            note=note,
        )
    )

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
        raise _withdraw_error("USER_ID_REQUIRED", 400)
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

        # Idempotent credit
        if status == "finished" and dep.status != "credited":
            _get_or_create_user_and_wallet(db, dep.user_id)

            w = db.query(Wallet).filter(Wallet.user_id == dep.user_id).with_for_update().one()
            w.balance_total = _round2(w.balance_total + dep.amount_usd)
            w.balance_available = _round2(w.balance_available + dep.amount_usd)

            tx = Transaction(
                user_id=dep.user_id,
                type="deposit",
                amount=float(dep.amount_usd),
                balance_after=float(w.balance_total),
                reference=payment_id,
            )
            db.add(tx)
            dep.status = "credited"
        else:
            if status:
                dep.status = status

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
        raise _withdraw_error("USER_ID_REQUIRED", 400)
    if amount <= 0:
        raise _withdraw_error("INVALID_AMOUNT", 400)
    if amount < MIN_WITHDRAW_USD:
        raise _withdraw_error("MIN_WITHDRAW_NOT_MET", 400)
    if not currency:
        raise _withdraw_error("PAYOUT_CURRENCY_REQUIRED", 400)
    if not address:
        raise _withdraw_error("PAYOUT_ADDRESS_REQUIRED", 400)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        # KYC CHECK
        user = db.query(User).filter(User.id == user_id).one()
        if user.kyc_status != "verified":
            raise _withdraw_error("KYC_REQUIRED", 403)
        if int(getattr(user, "kyc_level", 0) or 0) < 1:
            raise _withdraw_error("KYC_LEVEL_TOO_LOW", 403)
        if not bool(getattr(user, "auto_withdraw_enabled", False)):
            raise _withdraw_error("AUTO_WITHDRAW_DISABLED", 403)
        approved_limit = float(getattr(user, "auto_withdraw_limit", 0) or 0)
        if approved_limit > 0 and float(amount) > approved_limit:
            raise _withdraw_error("AUTO_WITHDRAW_LIMIT_EXCEEDED", 403)

        # Lock wallet row first (prevents concurrent withdrawal races)
        w = db.query(Wallet).filter(Wallet.user_id == user_id).with_for_update().one()

        # 🔒 Withdrawal lock: only one active withdrawal at a time
        existing_active = (
            db.query(Withdrawal)
            .filter(Withdrawal.user_id == user_id)
            .filter(Withdrawal.status.in_(ACTIVE_WITHDRAW_STATUSES))
            .count()
        )
        if existing_active > 0:
            raise _withdraw_error("WITHDRAWAL_ALREADY_ACTIVE", 400)

        if float(w.balance_available) < amount:
            raise _withdraw_error("INSUFFICIENT_AVAILABLE_BALANCE", 400)

        # Move funds: available -> pending
        w.balance_available = _round2(w.balance_available - amount)
        w.balance_pending = _round2(w.balance_pending + amount)

        wd = Withdrawal(
            user_id=user_id,
            amount_usd=float(amount),
            payout_currency=currency,
            payout_address=address,
            status="requested",
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
    wd.rejected_at = func.now()
    wd.note = reason
    _write_withdrawal_audit(db, wd.id, "rejected", actor_id="admin", note=reason)

# --------------------------
# Admin Withdrawals
# --------------------------

@app.get("/admin/withdrawals")
def admin_withdrawals(
    limit: int = 50,
    offset: int = 0,
    sort: str = "id_desc",
    status: str | None = None,
    user_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    viewer_id: str | None = None,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        q = db.query(Withdrawal)

        if scoped_ids is not None:
            q = q.filter(Withdrawal.user_id.in_(scoped_ids))

        if status:
            q = q.filter(Withdrawal.status == status)

        if user_id:
            q = q.filter(Withdrawal.user_id == user_id)

        if date_from:
            q = q.filter(Withdrawal.created_at >= date_from)

        if date_to:
            q = q.filter(Withdrawal.created_at <= date_to)

        total = q.count()

        sort_map = {
            "id_desc": Withdrawal.id.desc(),
            "id_asc": Withdrawal.id.asc(),
            "created_at_desc": Withdrawal.created_at.desc(),
            "created_at_asc": Withdrawal.created_at.asc(),
            "amount_desc": Withdrawal.amount_usd.desc(),
            "amount_asc": Withdrawal.amount_usd.asc(),
        }
        order_clause = sort_map.get(sort, Withdrawal.id.desc())

        rows = (
            q.order_by(order_clause)
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
            .all()
        )

        return {
            "total": total,
            "count": len(rows),
            "limit": limit,
            "offset": max(0, offset),
            "sort": sort,
            "withdrawals": [_serialize_withdrawal(w) for w in rows]
        }
    finally:
        db.close()



def _serialize_withdrawal_light(w):
    return {
        "id": w.id,
        "user_id": w.user_id,
        "amount_usd": float(w.amount_usd),
        "status": w.status,
        "created_at": str(w.created_at),
    }

def _queue_response(db, status, scoped_ids=None):
    q = db.query(Withdrawal).filter(Withdrawal.status == status)
    if scoped_ids is not None:
        q = q.filter(Withdrawal.user_id.in_(scoped_ids))

    rows = q.order_by(Withdrawal.id.asc()).all()

    count = len(rows)
    total_amount = round(sum(float(x.amount_usd or 0) for x in rows), 2)

    oldest = _serialize_withdrawal_light(rows[0]) if rows else None
    newest = _serialize_withdrawal_light(rows[-1]) if rows else None

    items = [_serialize_withdrawal_light(x) for x in rows[:50]]

    return {
        "status": status,
        "count": count,
        "total_amount": total_amount,
        "oldest": oldest,
        "newest": newest,
        "items": items
    }

@app.get("/admin/withdrawals/queue/requested")
def admin_queue_requested(viewer_id: str | None = None, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        return _queue_response(db, "requested", scoped_ids)
    finally:
        db.close()

@app.get("/admin/withdrawals/queue/approved")
def admin_queue_approved(viewer_id: str | None = None, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        return _queue_response(db, "approved", scoped_ids)
    finally:
        db.close()

@app.get("/admin/withdrawals/queue/sent")
def admin_queue_sent(viewer_id: str | None = None, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        return _queue_response(db, "sent", scoped_ids)
    finally:
        db.close()

@app.get("/admin/withdrawals/queue/failed")
def admin_queue_failed(viewer_id: str | None = None, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        return _queue_response(db, "failed", scoped_ids)
    finally:
        db.close()


@app.get("/admin/withdrawals/metrics")
def admin_withdrawals_metrics(
    viewer_id: str | None = None,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        statuses = ["requested", "approved", "sent", "completed", "rejected", "failed"]
        counts = {}
        for st in statuses:
            q = db.query(Withdrawal).filter(Withdrawal.status == st)
            if scoped_ids is not None:
                q = q.filter(Withdrawal.user_id.in_(scoped_ids))
            counts[st] = q.count()

        pending_statuses = ("requested", "approved", "sent")
        pending_q = db.query(Withdrawal).filter(Withdrawal.status.in_(pending_statuses))
        if scoped_ids is not None:
            pending_q = pending_q.filter(Withdrawal.user_id.in_(scoped_ids))
        pending_rows = pending_q.all()
        total_pending_amount = round(sum(float(x.amount_usd or 0) for x in pending_rows), 2)

        return {
            **counts,
            "total_pending_amount": total_pending_amount
        }
    finally:
        db.close()

@app.get("/admin/withdrawals/{withdrawal_id}")

def admin_withdrawal_get(withdrawal_id: int, viewer_id: str | None = None, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if viewer_id:
            _enforce_hierarchy_scope(db, viewer_id, w.user_id)

        audits = db.query(WithdrawalAuditLog).filter(
            WithdrawalAuditLog.withdrawal_id == withdrawal_id
        ).order_by(WithdrawalAuditLog.id.asc()).all()

        return {
            **_serialize_withdrawal(w),
            "audit_trail": [
                {
                    "id": a.id,
                    "action": a.action,
                    "actor_id": a.actor_id,
                    "note": a.note,
                    "created_at": str(a.created_at)
                }
                for a in audits
            ]
        }
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/approve")
async def admin_withdrawal_approve(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""

    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).with_for_update().one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if w.status == "approved":
            return {"ok": True, "withdrawal_id": w.id, "status": "approved"}
        if w.status != "requested":
            raise HTTPException(status_code=400, detail=f"Cannot approve withdrawal in status '{w.status}'")

        w.status = "approved"
        w.approved_at = func.now()
        w.approved_by = "admin"
        if note:
            w.note = note
        _write_withdrawal_audit(db, w.id, "approved", actor_id="admin", note=note or None)

        db.commit()
        return {"ok": True, "withdrawal_id": w.id, "status": w.status}
    finally:
        db.close()

@app.post("/admin/withdrawals/{withdrawal_id}/mark_sent")
async def admin_withdrawal_mark_sent(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""
    processor_ref = str(body.get("processor_ref", "")).strip() if isinstance(body, dict) else ""
    if not processor_ref:
        raise _withdraw_error("PROCESSOR_REF_REQUIRED", 400)

    db = SessionLocal()
    try:
        w = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).with_for_update().one_or_none()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if w.status == "sent":
            return {"ok": True, "withdrawal_id": w.id, "status": "sent"}
        if w.status != "approved":
            raise HTTPException(status_code=400, detail=f"Cannot mark sent from status '{w.status}'")

        w.status = "sent"
        w.sent_at = func.now()
        w.processor_ref = processor_ref
        if note:
            w.note = note
        _write_withdrawal_audit(db, w.id, "sent", actor_id="admin", note=note or None)

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
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).with_for_update().one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status == "completed":
            return {"ok": True, "withdrawal_id": wd.id, "status": "completed"}
        if wd.status != "sent":
            raise HTTPException(status_code=400, detail=f"Cannot complete withdrawal from status '{wd.status}'")

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
        wd.completed_at = func.now()
        if note:
            wd.note = note
        _write_withdrawal_audit(db, wd.id, "completed", actor_id="admin", note=note or None)

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
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).with_for_update().one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status == "rejected":
            return {"ok": True, "withdrawal_id": wd.id, "status": "rejected", "refunded": bool(wd.refunded)}
        if wd.status not in ("requested", "approved"):
            raise HTTPException(status_code=400, detail=f"Cannot reject withdrawal in status '{wd.status}'")

        _get_or_create_user_and_wallet(db, wd.user_id)
        _refund_withdrawal(db, wd, reason=reason)

        db.commit()
        return {"ok": True, "withdrawal_id": wd.id, "status": wd.status, "refunded": bool(wd.refunded)}
    finally:
        db.close()


@app.post("/admin/withdrawals/{withdrawal_id}/fail")
async def admin_withdrawal_fail(withdrawal_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """
    Fail after send:
    pending -> available
    total unchanged
    """
    _require_admin(x_admin_key)
    body = await request.json()
    reason = str(body.get("reason", "")).strip() if isinstance(body, dict) else ""
    if not reason:
        reason = "Withdrawal payout failed"

    db = SessionLocal()
    try:
        wd = db.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).with_for_update().one_or_none()
        if not wd:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if wd.status == "failed":
            return {"ok": True, "withdrawal_id": wd.id, "status": "failed", "refunded": bool(wd.refunded)}
        if wd.status != "sent":
            raise HTTPException(status_code=400, detail=f"Cannot fail withdrawal in status '{wd.status}'")

        _get_or_create_user_and_wallet(db, wd.user_id)
        w = db.query(Wallet).filter(Wallet.user_id == wd.user_id).with_for_update().one()

        w.balance_pending = _round2(w.balance_pending - wd.amount_usd)
        w.balance_available = _round2(w.balance_available + wd.amount_usd)

        if w.balance_pending < -0.000001:
            raise HTTPException(status_code=500, detail="Wallet pending balance went negative (data mismatch)")

        tx = Transaction(
            user_id=wd.user_id,
            type="withdrawal_failed_refund",
            amount=float(wd.amount_usd),
            balance_after=float(w.balance_total),
            reference=f"withdrawal:{wd.id}",
        )
        db.add(tx)

        wd.refunded = 1
        wd.status = "failed"
        wd.failed_at = func.now()
        wd.failure_reason = reason
        wd.note = reason
        _write_withdrawal_audit(db, wd.id, "failed", actor_id="admin", note=reason)

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


@app.get("/admin/users/{user_id}/profile")
def admin_user_profile(user_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        user_row = db.execute(sa_text("""
            SELECT
                u.id,
                COALESCE(cu.email, u.email) AS email,
                cu.username,
                cu.full_name,
                cu.telegram,
                cu.phone,
                cu.notes,
                u.role,
                u.parent_id,
                u.created_by,
                u.agent_code,
                COALESCE(cu.is_active, u.is_active, TRUE) AS is_active,
                u.created_at,
                u.kyc_status,
                u.kyc_level,
                u.auto_withdraw_enabled,
                u.auto_withdraw_limit,
                u.kyc_verified_at,
                u.kyc_rejected_reason,
                u.kyc_approved_by,
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
                u.last_created_by_change_by
            FROM users u
            LEFT JOIN c2w_users cu
                ON cu.user_id = u.id
            WHERE u.id = :user_id
            LIMIT 1
        """), {"user_id": user_id}).mappings().first()

        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        wallet_row = db.execute(sa_text("""
            SELECT
                user_id,
                COALESCE(balance_total, 0) AS balance_total,
                COALESCE(balance_available, 0) AS balance_available,
                COALESCE(balance_pending, 0) AS balance_pending,
                updated_at
            FROM wallets
            WHERE user_id = :user_id
            LIMIT 1
        """), {"user_id": user_id}).mappings().first()

        deposit_stats = db.execute(sa_text("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount_usd), 0) AS total_amount,
                COALESCE(SUM(
                    CASE
                        WHEN LOWER(COALESCE(status, '')) IN ('credited', 'paid', 'completed')
                        THEN amount_usd ELSE 0
                    END
                ), 0) AS credited_amount
            FROM deposits
            WHERE user_id = :user_id
        """), {"user_id": user_id}).mappings().first()

        withdrawal_stats = db.execute(sa_text("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount_usd), 0) AS total_amount,
                COALESCE(SUM(
                    CASE
                        WHEN LOWER(COALESCE(status, '')) = 'completed'
                        THEN amount_usd ELSE 0
                    END
                ), 0) AS completed_amount,
                COALESCE(SUM(
                    CASE
                        WHEN LOWER(COALESCE(status, '')) IN ('requested', 'approved', 'sent')
                        THEN amount_usd ELSE 0
                    END
                ), 0) AS pending_amount
            FROM withdrawals
            WHERE user_id = :user_id
        """), {"user_id": user_id}).mappings().first()

        recent_transactions = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .order_by(Transaction.id.desc())
            .limit(20)
            .all()
        )

        recent_deposits = (
            db.query(Deposit)
            .filter(Deposit.user_id == user_id)
            .order_by(Deposit.id.desc())
            .limit(10)
            .all()
        )

        recent_withdrawals = (
            db.query(Withdrawal)
            .filter(Withdrawal.user_id == user_id)
            .order_by(Withdrawal.id.desc())
            .limit(10)
            .all()
        )

        return {
            "ok": True,
            "profile": {
                "user": {
                    "id": user_row["id"],
                    "email": user_row["email"],
                    "username": user_row["username"],
                    "full_name": user_row["full_name"],
                    "telegram": user_row["telegram"],
                    "phone": user_row["phone"],
                    "notes": user_row["notes"],
                    "role": user_row["role"],
                    "parent_id": user_row["parent_id"],
                    "created_by": user_row["created_by"],
                    "agent_code": user_row["agent_code"],
                    "is_active": bool(user_row["is_active"]) if user_row["is_active"] is not None else True,
                    "created_at": user_row["created_at"].isoformat() if user_row["created_at"] else None,
                    "kyc_status": user_row["kyc_status"] or "unverified",
                    "kyc_level": int(user_row["kyc_level"] or 0),
                    "auto_withdraw_enabled": bool(user_row["auto_withdraw_enabled"]) if user_row["auto_withdraw_enabled"] is not None else False,
                    "auto_withdraw_limit": float(user_row["auto_withdraw_limit"] or 0),
                    "kyc_verified_at": user_row["kyc_verified_at"].isoformat() if user_row["kyc_verified_at"] else None,
                    "kyc_rejected_reason": user_row["kyc_rejected_reason"],
                    "kyc_approved_by": user_row["kyc_approved_by"],
                    "billing_type": user_row["billing_type"],
                    "pph_rate": float(user_row["pph_rate"] or 0),
                    "ggr_share": float(user_row["ggr_share"] or 0),
                    "service_pph": float(user_row["service_pph"] or 0),
                    "service_ggr": float(user_row["service_ggr"] or 0),
                    "originals_ggr": float(user_row["originals_ggr"] or 0),
                    "casino_ggr": float(user_row["casino_ggr"] or 0),
                    "live_betting_ggr": float(user_row["live_betting_ggr"] or 0),
                    "updated_at": user_row["updated_at"].isoformat() if user_row["updated_at"] else None,
                    "updated_by": user_row["updated_by"],
                    "last_parent_change_at": user_row["last_parent_change_at"].isoformat() if user_row["last_parent_change_at"] else None,
                    "last_parent_change_by": user_row["last_parent_change_by"],
                    "last_agent_code_change_at": user_row["last_agent_code_change_at"].isoformat() if user_row["last_agent_code_change_at"] else None,
                    "last_agent_code_change_by": user_row["last_agent_code_change_by"],
                    "last_created_by_change_at": user_row["last_created_by_change_at"].isoformat() if user_row["last_created_by_change_at"] else None,
                    "last_created_by_change_by": user_row["last_created_by_change_by"],
                },
                "wallet": {
                    "balance_total": float((wallet_row["balance_total"] if wallet_row else 0) or 0),
                    "balance_available": float((wallet_row["balance_available"] if wallet_row else 0) or 0),
                    "balance_pending": float((wallet_row["balance_pending"] if wallet_row else 0) or 0),
                    "updated_at": wallet_row["updated_at"].isoformat() if wallet_row and wallet_row["updated_at"] else None,
                },
                "deposit_stats": {
                    "total_count": int(deposit_stats["total_count"] or 0),
                    "total_amount": float(deposit_stats["total_amount"] or 0),
                    "credited_amount": float(deposit_stats["credited_amount"] or 0),
                },
                "withdrawal_stats": {
                    "total_count": int(withdrawal_stats["total_count"] or 0),
                    "total_amount": float(withdrawal_stats["total_amount"] or 0),
                    "completed_amount": float(withdrawal_stats["completed_amount"] or 0),
                    "pending_amount": float(withdrawal_stats["pending_amount"] or 0),
                },
                "recent_transactions": [_serialize_tx(t) for t in recent_transactions],
                "recent_deposits": [_serialize_deposit(d) for d in recent_deposits],
                "recent_withdrawals": [_serialize_withdrawal(w) for w in recent_withdrawals],
            }
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
    Hardened against overflow / inf / NaN.
    """
    safe_elapsed = max(0.0, min(float(elapsed_seconds or 0.0), 60.0))
    exp_input = min(safe_elapsed * 0.16, 50.0)
    try:
        value = math.exp(exp_input)
    except OverflowError:
        value = math.exp(50.0)
    if not math.isfinite(value):
        value = 1.0
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
            user_id=bet.user_id,
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


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _global_crash_live_multiplier_from_elapsed(elapsed_seconds: float) -> float:
    """
    Smooth crash pacing:
    ~1.38x at 2s, ~1.90x at 4s, ~2.61x at 6s, ~4.95x at 10s
    Hardened against overflow / inf / NaN.
    """
    safe_elapsed = max(0.0, min(float(elapsed_seconds or 0.0), 60.0))
    exp_input = min(safe_elapsed * 0.16, 50.0)
    try:
        value = math.exp(exp_input)
    except OverflowError:
        value = math.exp(50.0)
    if not math.isfinite(value):
        value = 1.0
    return round(max(1.0, value), 2)


def _ensure_aware(dt):
    from datetime import timezone
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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




# --------------------------
# SoftSwiss Backoffice / Games
# --------------------------
@app.get("/admin/softswiss/games")
def admin_softswiss_games(
    q: str | None = None,
    category: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    safe_page = max(1, int(page or 1))
    safe_limit = max(1, min(int(limit or 50), 200))
    offset = (safe_page - 1) * safe_limit

    where = ["1=1"]
    params = {"limit": safe_limit, "offset": offset}

    q = str(q or "").strip()
    if q:
        where.append("(title ILIKE :q OR provider_game_id ILIKE :q OR category ILIKE :q)")
        params["q"] = f"%{q}%"

    category = str(category or "").strip()
    if category and category != "all":
        where.append("category = :category")
        params["category"] = category

    status = str(status or "").strip().lower()
    if status == "enabled":
        where.append("is_enabled = TRUE")
    elif status == "disabled":
        where.append("is_enabled = FALSE")
    elif status == "featured":
        where.append("is_featured = TRUE")
    elif status == "live":
        where.append("is_live = TRUE")
    elif status == "missing-thumbnail":
        where.append("(image_url IS NULL OR image_url = '')")

    where_sql = " AND ".join(where)

    db = SessionLocal()
    try:
        total = db.execute(text(f"""
            SELECT COUNT(*)
            FROM softswiss_games
            WHERE {where_sql}
        """), params).scalar() or 0

        rows = db.execute(text(f"""
            SELECT id, provider_game_id, title, category, image_url,
                   is_enabled, is_featured, is_live, sort_order, created_at, updated_at
            FROM softswiss_games
            WHERE {where_sql}
            ORDER BY is_featured DESC, sort_order ASC, title ASC, id DESC
            LIMIT :limit OFFSET :offset
        """), params).mappings().all()

        return {
            "ok": True,
            "count": len(rows),
            "total": int(total or 0),
            "page": safe_page,
            "limit": safe_limit,
            "pages": int(((int(total or 0) + safe_limit - 1) // safe_limit) or 1),
            "games": [dict(r) for r in rows],
        }
    finally:
        db.close()






@app.post("/admin/softswiss/games/bulk-update-filtered")
async def admin_softswiss_games_bulk_update_filtered(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()

    q = str(body.get("q") or "").strip()
    category = str(body.get("category") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    dry_run = bool(body.get("dry_run", False))

    allowed = {
        "category": body.get("set_category"),
        "is_enabled": body.get("is_enabled"),
        "is_featured": body.get("is_featured"),
        "is_live": body.get("is_live"),
    }
    allowed = {k: v for k, v in allowed.items() if v is not None}

    if not dry_run and not allowed:
        raise HTTPException(status_code=400, detail="no supported bulk fields provided")

    where = ["1=1"]
    params = {}

    if q:
        where.append("(title ILIKE :q OR provider_game_id ILIKE :q OR category ILIKE :q)")
        params["q"] = f"%{q}%"

    if category and category != "all":
        where.append("category = :category_filter")
        params["category_filter"] = category

    if status == "enabled":
        where.append("is_enabled = TRUE")
    elif status == "disabled":
        where.append("is_enabled = FALSE")
    elif status == "featured":
        where.append("is_featured = TRUE")
    elif status == "live":
        where.append("is_live = TRUE")
    elif status == "missing-thumbnail":
        where.append("(image_url IS NULL OR image_url = '')")

    where_sql = " AND ".join(where)

    db = SessionLocal()
    try:
        total = db.execute(text(f"""
            SELECT COUNT(*)
            FROM softswiss_games
            WHERE {where_sql}
        """), params).scalar() or 0

        if dry_run:
            return {"ok": True, "dry_run": True, "matched": int(total or 0)}

        if int(total or 0) <= 0:
            return {"ok": True, "updated": 0, "matched": 0}

        sets = []
        update_params = dict(params)
        for k, v in allowed.items():
            sets.append(f"{k}=:{k}")
            update_params[k] = v
        sets.append("updated_at=NOW()")

        result = db.execute(text(f"""
            UPDATE softswiss_games
            SET {", ".join(sets)}
            WHERE {where_sql}
        """), update_params)

        db.commit()
        return {"ok": True, "matched": int(total or 0), "updated": int(result.rowcount or 0)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/admin/softswiss/games/bulk-update")
async def admin_softswiss_games_bulk_update(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()

    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be non-empty list")

    ids = [int(x) for x in ids if str(x).strip().isdigit()]
    if not ids:
        raise HTTPException(status_code=400, detail="valid ids required")

    allowed = {
        "category": body.get("category"),
        "is_enabled": body.get("is_enabled"),
        "is_featured": body.get("is_featured"),
        "is_live": body.get("is_live"),
    }
    allowed = {k: v for k, v in allowed.items() if v is not None}

    if not allowed:
        raise HTTPException(status_code=400, detail="no supported bulk fields provided")

    sets = []
    params = {"ids": ids}
    for k, v in allowed.items():
        sets.append(f"{k}=:{k}")
        params[k] = v
    sets.append("updated_at=NOW()")

    db = SessionLocal()
    try:
        result = db.execute(text(f"""
            UPDATE softswiss_games
            SET {", ".join(sets)}
            WHERE id = ANY(:ids)
        """), params)
        db.commit()
        return {"ok": True, "updated": int(result.rowcount or 0)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/admin/softswiss/games/{game_id}/update")
async def admin_softswiss_game_update(game_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()

    allowed = {
        "title": body.get("title"),
        "category": body.get("category"),
        "image_url": body.get("image_url"),
        "is_enabled": body.get("is_enabled"),
        "is_featured": body.get("is_featured"),
        "is_live": body.get("is_live"),
        "sort_order": body.get("sort_order"),
    }
    allowed = {k: v for k, v in allowed.items() if v is not None}

    if not allowed:
        return {"ok": True, "updated": False}

    sets = []
    params = {"game_id": game_id}
    for k, v in allowed.items():
        sets.append(f"{k}=:{k}")
        params[k] = v
    sets.append("updated_at=NOW()")

    db = SessionLocal()
    try:
        db.execute(text(f"""
            UPDATE softswiss_games
            SET {", ".join(sets)}
            WHERE id=:game_id
        """), params)
        db.commit()
        return {"ok": True, "updated": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()




@app.post("/admin/softswiss/games/sync")
async def admin_softswiss_games_sync(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)

    if not SOFTSWISS_ENABLED:
        return {
            "ok": True,
            "skipped": True,
            "reason": "SOFTSWISS_ENABLED=false",
            "upserted": 0,
        }

    if not SOFTSWISS_BASE_URL or not SOFTSWISS_CASINO_ID or not SOFTSWISS_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="SoftSwiss config missing")

    payload = {
        "casino_id": SOFTSWISS_CASINO_ID,
    }

    raw_body = _softswiss_compact_json(payload)
    signature = _softswiss_sign_body(raw_body)

    try:
        res = requests.post(
            f"{SOFTSWISS_BASE_URL}/v2/casino_a8r.Game/List",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-REQUEST-SIGN": signature,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"SoftSwiss Game/List request failed: {str(e)}")

    try:
        data = res.json()
    except Exception:
        data = {"raw": res.text}

    if res.status_code >= 400:
        raise HTTPException(status_code=502, detail={
            "message": "SoftSwiss Game/List rejected",
            "status": res.status_code,
            "response": data,
        })

    raw_games = data.get("games") or data.get("items") or data.get("data") or []
    if isinstance(raw_games, dict):
        raw_games = raw_games.get("games") or raw_games.get("items") or []

    if not isinstance(raw_games, list):
        raise HTTPException(status_code=502, detail={
            "message": "SoftSwiss Game/List response has no games list",
            "response": data,
        })

    db = SessionLocal()
    try:
        count = 0
        for g in raw_games:
            if not isinstance(g, dict):
                continue

            provider_game_id = str(
                g.get("id")
                or g.get("identifier")
                or g.get("game_id")
                or g.get("provider_game_id")
                or ""
            ).strip()

            title = str(g.get("title") or g.get("name") or provider_game_id).strip()
            if not provider_game_id or not title:
                continue

            raw_category = str(g.get("category") or g.get("type") or "").strip().lower()
            is_live = bool(g.get("is_live") or raw_category in ("live", "live-casino", "live_casino"))
            category = "live" if is_live else (raw_category or "slots")
            if category in ("slot", "video_slots", "video-slots"):
                category = "slots"

            image_url = (
                g.get("image_url")
                or g.get("image")
                or g.get("thumbnail")
                or g.get("icon")
                or ""
            )

            db.execute(text("""
                INSERT INTO softswiss_games
                (provider_game_id, title, category, image_url, image_status, image_note, is_enabled, is_featured, is_live, sort_order, raw_payload)
                VALUES
                (:provider_game_id, :title, :category, :image_url, :image_status, :image_note, FALSE, FALSE, :is_live, 0, CAST(:raw_payload AS jsonb))
                ON CONFLICT (provider_game_id)
                DO UPDATE SET
                  title=EXCLUDED.title,
                  category=EXCLUDED.category,
                  image_url=COALESCE(NULLIF(EXCLUDED.image_url, ''), softswiss_games.image_url),
                  image_status=EXCLUDED.image_status,
                  image_note=EXCLUDED.image_note,
                  is_live=EXCLUDED.is_live,
                  raw_payload=EXCLUDED.raw_payload,
                  updated_at=NOW()
            """), {
                "provider_game_id": provider_game_id,
                "title": title,
                "category": category,
                "image_url": image_url,
                "image_status": "provider" if str(image_url or "").strip() else "missing",
                "image_note": "Provider/remote image" if str(image_url or "").strip() else "No thumbnail URL from provider",
                "is_live": is_live,
                "raw_payload": json.dumps(g),
            })
            count += 1

        db.commit()
        return {"ok": True, "skipped": False, "upserted": count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/admin/softswiss/games/manual-seed")
async def admin_softswiss_games_manual_seed(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()
    games = body.get("games") or []
    if not isinstance(games, list):
        raise HTTPException(status_code=400, detail="games must be list")

    db = SessionLocal()
    try:
        count = 0
        for g in games:
            provider_game_id = str(g.get("provider_game_id") or g.get("id") or "").strip()
            title = str(g.get("title") or g.get("name") or provider_game_id).strip()
            if not provider_game_id or not title:
                continue

            db.execute(text("""
                INSERT INTO softswiss_games
                (provider_game_id, title, category, image_url, is_enabled, is_featured, is_live, sort_order, raw_payload)
                VALUES
                (:provider_game_id, :title, :category, :image_url, :is_enabled, :is_featured, :is_live, :sort_order, CAST(:raw_payload AS jsonb))
                ON CONFLICT (provider_game_id)
                DO UPDATE SET
                  title=EXCLUDED.title,
                  category=EXCLUDED.category,
                  image_url=EXCLUDED.image_url,
                  raw_payload=EXCLUDED.raw_payload,
                  updated_at=NOW()
            """), {
                "provider_game_id": provider_game_id,
                "title": title,
                "category": str(g.get("category") or "slots"),
                "image_url": g.get("image_url"),
                "is_enabled": bool(g.get("is_enabled", False)),
                "is_featured": bool(g.get("is_featured", False)),
                "is_live": bool(g.get("is_live", False)),
                "sort_order": int(g.get("sort_order") or 0),
                "raw_payload": json.dumps(g),
            })
            count += 1
        db.commit()
        return {"ok": True, "upserted": count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()






@app.post("/casino/softswiss/launch")
async def public_softswiss_launch(
    request: Request,
    x_internal_launch_key: str | None = Header(default=None, alias="X-Internal-Launch-Key"),
):
    expected_launch_key = os.getenv("SOFTSWISS_LAUNCH_INTERNAL_KEY", ADMIN_KEY).strip()
    if expected_launch_key and str(x_internal_launch_key or "").strip() != expected_launch_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    game_id = str(body.get("game_id") or "").strip()
    mode = str(body.get("mode") or "real").lower().strip()

    if not user_id:
        raise HTTPException(status_code=401, detail="authenticated user required")
    if not game_id:
        raise HTTPException(status_code=400, detail="game_id required")

    db = SessionLocal()
    try:
        game = db.execute(text("""
            SELECT provider_game_id, title, is_enabled
            FROM softswiss_games
            WHERE provider_game_id = :game_id
            LIMIT 1
        """), {"game_id": game_id}).mappings().first()

        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        if not game["is_enabled"]:
            raise HTTPException(status_code=403, detail="Game disabled")

        session_payload = str(uuid.uuid4())

        db.execute(text("""
            INSERT INTO softswiss_sessions
            (session_payload, user_id, game_id, provider, currency, locale, jurisdiction, status)
            VALUES
            (:session_payload, :user_id, :game_id, 'softswiss', :currency, :locale, :jurisdiction, 'active')
            ON CONFLICT (session_payload) DO NOTHING
        """), {
            "session_payload": session_payload,
            "user_id": user_id,
            "game_id": game_id,
            "currency": SOFTSWISS_DEFAULT_CURRENCY,
            "locale": SOFTSWISS_DEFAULT_LOCALE,
            "jurisdiction": SOFTSWISS_DEFAULT_JURISDICTION,
        })
        db.commit()

        if not SOFTSWISS_ENABLED:
            launch_url = f"/casino/mock-game?game_id={game_id}&session_payload={session_payload}&mode={mode}"
            return {
                "ok": True,
                "mode": mode,
                "game_id": game_id,
                "title": game["title"],
                "session_payload": session_payload,
                "launch_url": launch_url,
                "mock": True,
            }

        if not SOFTSWISS_BASE_URL or not SOFTSWISS_CASINO_ID or not SOFTSWISS_AUTH_TOKEN:
            raise HTTPException(status_code=500, detail="SoftSwiss real launcher config missing")

        launch_payload = {
            "casino_id": SOFTSWISS_CASINO_ID,
            "game": game_id,
            "player": {
                "id": user_id,
                "currency": SOFTSWISS_DEFAULT_CURRENCY,
            },
            "session_payload": session_payload,
            "locale": SOFTSWISS_DEFAULT_LOCALE,
            "jurisdiction": SOFTSWISS_DEFAULT_JURISDICTION,
            "return_url": SOFTSWISS_RETURN_URL,
            "deposit_url": SOFTSWISS_DEPOSIT_URL,
        }

        raw_body = _softswiss_compact_json(launch_payload)
        signature = _softswiss_sign_body(raw_body)

        res = requests.post(
            f"{SOFTSWISS_BASE_URL}/v2/casino_a8r.Game/Launcher/Real",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-REQUEST-SIGN": signature,
            },
            timeout=15,
        )

        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text}

        if res.status_code >= 400:
            raise HTTPException(status_code=502, detail={"message": "SoftSwiss launcher rejected", "status": res.status_code, "response": data})

        launch_url = data.get("launch_url") or data.get("url") or data.get("game_url")
        if not launch_url:
            raise HTTPException(status_code=502, detail={"message": "SoftSwiss launcher response missing URL", "response": data})

        return {
            "ok": True,
            "mode": mode,
            "game_id": game_id,
            "title": game["title"],
            "session_payload": session_payload,
            "launch_url": launch_url,
            "mock": False,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SoftSwiss launch failed: {str(e)}")
    finally:
        db.close()


@app.get("/casino/softswiss/games")
def public_softswiss_games(category: str | None = None, limit: int = 100):
    safe_limit = max(1, min(int(limit or 100), 500))
    db = SessionLocal()
    try:
        params = {"limit": safe_limit}
        where = "WHERE is_enabled = true"

        if category:
            where += " AND category = :category"
            params["category"] = str(category).strip()

        rows = db.execute(text(f"""
            SELECT provider_game_id, title, category, image_url,
                   is_featured, is_live, sort_order
            FROM softswiss_games
            {where}
            ORDER BY is_featured DESC, sort_order ASC, title ASC
            LIMIT :limit
        """), params).mappings().all()

        return {
            "ok": True,
            "count": len(rows),
            "games": [dict(r) for r in rows],
        }
    finally:
        db.close()


@app.get("/activity/wins")
def public_activity_wins(limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(Transaction)
            .filter(Transaction.type.in_(["dice_payout", "crash_payout", "coinflip_payout", "mines_payout", "hilo_payout", "softswiss_win"]))
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
            elif tx_type == "coinflip_payout":
                game = "Coinflip"
                event_type = "win"
            elif tx_type == "mines_payout":
                game = "Mines"
                event_type = "cashout"
            elif tx_type == "hilo_payout":
                game = "Hi-Lo"
                event_type = "cashout"
            elif tx_type == "softswiss_win":
                game = "Casino"
                event_type = "win"
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

    viewer_id = request.query_params.get("viewer_id")

    if not viewer_id:

        raise HTTPException(status_code=400, detail="viewer_id required")

    _enforce_hierarchy_scope(db, viewer_id, user_id)
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
def admin_kyc_users(viewer_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, viewer_id)
        tree = _get_subtree_rows(db, viewer_id)
        ids = [r["id"] for r in tree]
        rows = db.execute(
            text("""
                SELECT
                    id,
                    role,
                    kyc_status,
                    kyc_level,
                    auto_withdraw_enabled,
                    auto_withdraw_limit,
                    kyc_verified_at,
                    kyc_reviewed_at,
                    kyc_rejected_at,
                    kyc_approved_by,
                    kyc_rejected_reason,
                    created_at
                FROM users
                WHERE id = ANY(:ids)
                ORDER BY created_at DESC
            """),
            {"ids": ids},
        ).mappings().all()

        return {
            "ok": True,
            "count": len(rows),
            "users": [
                {
                    "id": r["id"],
                    "role": r["role"],
                    "kyc_status": r["kyc_status"] or "unverified",
                    "kyc_level": int(r["kyc_level"] or 0),
                    "auto_withdraw_enabled": bool(r["auto_withdraw_enabled"]) if r["auto_withdraw_enabled"] is not None else False,
                    "auto_withdraw_limit": float(r["auto_withdraw_limit"] or 0),
                    "kyc_verified_at": r["kyc_verified_at"].isoformat() if r["kyc_verified_at"] else None,
                    "kyc_reviewed_at": r["kyc_reviewed_at"].isoformat() if r["kyc_reviewed_at"] else None,
                    "kyc_rejected_at": r["kyc_rejected_at"].isoformat() if r["kyc_rejected_at"] else None,
                    "kyc_approved_by": r["kyc_approved_by"],
                    "kyc_rejected_reason": r["kyc_rejected_reason"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@app.post("/admin/kyc/users/{user_id}/approve")
async def admin_kyc_user_approve(user_id: str, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    viewer_id = request.query_params.get("viewer_id")
    if not viewer_id:
        raise HTTPException(status_code=400, detail="viewer_id required")

    body = await request.json()
    level = int(body.get("level", 0) or 0)
    if level not in (1, 2):
        raise HTTPException(status_code=400, detail="level must be 1 or 2")

    approved_limit = float(KYC_LEVEL_LIMITS[level])

    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, user_id)
        user = db.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.kyc_status = "verified"
        user.kyc_level = level
        user.auto_withdraw_enabled = True
        user.auto_withdraw_limit = approved_limit
        user.kyc_verified_at = func.now()
        user.kyc_reviewed_at = func.now()
        user.kyc_approved_by = "admin"
        user.kyc_rejected_at = None
        user.kyc_rejected_reason = None

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "kyc_status": "verified",
            "kyc_level": level,
            "auto_withdraw_enabled": True,
            "auto_withdraw_limit": approved_limit,
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

    viewer_id = request.query_params.get("viewer_id")
    if not viewer_id:
        raise HTTPException(status_code=400, detail="viewer_id required")

    body = await request.json()
    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")

    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, user_id)
        user = db.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.kyc_status = "rejected"
        user.kyc_level = 0
        user.auto_withdraw_enabled = False
        user.auto_withdraw_limit = 0
        user.kyc_verified_at = None
        user.kyc_reviewed_at = func.now()
        user.kyc_rejected_at = func.now()
        user.kyc_approved_by = None
        user.kyc_rejected_reason = reason

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "kyc_status": "rejected",
            "kyc_level": 0,
            "auto_withdraw_enabled": False,
            "auto_withdraw_limit": 0,
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
def admin_kyc_files(user_id: str, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    viewer_id = request.query_params.get("viewer_id")
    if not viewer_id:
        raise HTTPException(status_code=400, detail="viewer_id required")

    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, user_id)

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
    finally:
        db.close()


@app.get("/admin/kyc/file/{user_id}/{filename}")
def admin_kyc_file(user_id: str, filename: str, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    user_id = str(user_id).strip()
    filename = str(filename).strip()

    if not user_id or not filename:
        raise HTTPException(status_code=400, detail="Invalid file request")

    viewer_id = request.query_params.get("viewer_id")
    if not viewer_id:
        raise HTTPException(status_code=400, detail="viewer_id required")

    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, user_id)

        target = KYC_UPLOAD_DIR / filename

        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        if not target.name.startswith(f"{user_id}_"):
            raise HTTPException(status_code=403, detail="Forbidden")

        return FileResponse(str(target), filename=target.name)
    finally:
        db.close()



# =========================
# KYC DETAIL (UNIFIED)
# =========================

@app.get("/admin/kyc/users/{user_id}")
def admin_kyc_user_detail(user_id: str, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)

    user_id = str(user_id).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    viewer_id = request.query_params.get("viewer_id")
    if not viewer_id:
        raise HTTPException(status_code=400, detail="viewer_id required")

    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, user_id)

        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        files = {
            "id_document": None,
            "selfie": None,
            "proof_of_address": None,
        }

        if KYC_UPLOAD_DIR.exists():
            for pth in KYC_UPLOAD_DIR.iterdir():
                if not pth.is_file():
                    continue
                if not pth.name.startswith(f"{user_id}_"):
                    continue

                low = pth.name.lower()
                url = f"/api/admin/kyc/file/{user_id}/{pth.name}"

                if "front" in low or "id" in low:
                    files["id_document"] = url
                elif "selfie" in low:
                    files["selfie"] = url
                elif "poa" in low or "address" in low:
                    files["proof_of_address"] = url

        return {
            "ok": True,
            "user_id": user.id,
            "role": getattr(user, "role", None),
            "kyc_status": getattr(user, "kyc_status", "unverified"),
            "kyc_level": getattr(user, "kyc_level", 0),
            "auto_withdraw_enabled": getattr(user, "auto_withdraw_enabled", False),
            "auto_withdraw_limit": float(getattr(user, "auto_withdraw_limit", 0) or 0),
            "kyc_reviewed_at": user.kyc_reviewed_at.isoformat() if getattr(user, "kyc_reviewed_at", None) else None,
            "kyc_verified_at": user.kyc_verified_at.isoformat() if getattr(user, "kyc_verified_at", None) else None,
            "kyc_rejected_at": user.kyc_rejected_at.isoformat() if getattr(user, "kyc_rejected_at", None) else None,
            "kyc_approved_by": getattr(user, "kyc_approved_by", None),
            "kyc_rejected_reason": getattr(user, "kyc_rejected_reason", None),
            "created_at": user.created_at.isoformat() if getattr(user, "created_at", None) else None,
            "files": files,
        }
    finally:
        db.close()


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



def _normalize_role(role: str | None) -> str:
    r = str(role or "").strip().lower()
    if r == "superadmin":
        return "super_admin"
    return r


def _can_view_admin_hierarchy(role: str | None) -> bool:
    r = _normalize_role(role)
    return r in {"super_admin", "admin", "master_agent", "agent", "sub_agent"}


def _can_create_child_role(parent_role: str, child_role: str) -> bool:
    parent_role = str(parent_role or "").strip().lower()
    child_role = str(child_role or "").strip().lower()
    return child_role in CHILD_ROLE_RULES.get(parent_role, set())



def _get_subtree_rows(db, root_user_id: str):
    rows = db.execute(sa_text("""
        WITH RECURSIVE tree AS (
            SELECT id, parent_id, role, 0 as depth
            FROM users
            WHERE id = :root_id

            UNION ALL

            SELECT u.id, u.parent_id, u.role, t.depth + 1
            FROM users u
            JOIN tree t ON u.parent_id = t.id
        )
        SELECT * FROM tree
        ORDER BY depth, id
    """), {"root_id": root_user_id}).mappings().all()

    return [dict(r) for r in rows]
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

        tree = _get_subtree_rows(db, viewer_id)
        return {
            "ok": True,
            "viewer_id": viewer_id,
            "viewer_role": viewer_role,
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


@app.get("/admin/users")
def admin_users(viewer_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, viewer_id)
        tree = _get_subtree_rows(db, viewer_id)
        ids = [r["id"] for r in tree]
        rows = db.execute(
            text("""
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
                WHERE id = ANY(:ids)
                ORDER BY created_at DESC
            """),
            {"ids": ids},
        ).mappings().all()

        return {
            "ok": True,
            "count": len(rows),
            "users": [
                {
                    "id": r["id"],
                    "role": r["role"],
                    "parent_id": r["parent_id"],
                    "created_by": r["created_by"],
                    "agent_code": r["agent_code"],
                    "is_active": bool(r["is_active"]) if r["is_active"] is not None else True,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "billing_type": r["billing_type"],
                    "pph_rate": float(r["pph_rate"] or 0),
                    "ggr_share": float(r["ggr_share"] or 0),
                    "service_pph": float(r["service_pph"] or 0),
                    "service_ggr": float(r["service_ggr"] or 0),
                    "originals_ggr": float(r["originals_ggr"] or 0),
                    "casino_ggr": float(r["casino_ggr"] or 0),
                    "live_betting_ggr": float(r["live_betting_ggr"] or 0),
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@app.get("/admin/crm/low-balance/{viewer_id}")
def admin_crm_low_balance(
    viewer_id: str,
    threshold: float = 10,
    segment: str = 'low_balance',
    days: int = 7,
    x_admin_key: str | None = Header(default=None, alias='X-Admin-Key'),
):
    db = SessionLocal()
    try:
        _enforce_hierarchy_scope(db, viewer_id, viewer_id)
        tree = _get_subtree_rows(db, viewer_id)
        player_ids = [str(r["id"]) for r in tree if r.get("id") and str(r.get("role") or "").strip().lower() == "player"]

        if not player_ids:
            return {
                "ok": True,
                "viewer_id": viewer_id,
                "threshold": float(threshold),
                "count": 0,
                "items": [],
            }
        segment = (segment or 'low_balance').lower().strip()
        days = max(1, min(int(days or 7), 365))

        extra_where = ''

        if segment == 'no_bet':
            extra_where = "AND (lb.last_bet_at IS NULL OR lb.last_bet_at < NOW() - INTERVAL '%s days')" % days
        elif segment == 'no_deposit':
            extra_where = "AND (ld.last_deposit_at IS NULL OR ld.last_deposit_at < NOW() - INTERVAL '%s days')" % days
        elif segment == 'inactive':
            extra_where = "AND ( (lb.last_bet_at IS NULL OR lb.last_bet_at < NOW() - INTERVAL '%s days') AND (ld.last_deposit_at IS NULL OR ld.last_deposit_at < NOW() - INTERVAL '%s days') AND (lw.last_withdrawal_at IS NULL OR lw.last_withdrawal_at < NOW() - INTERVAL '%s days') )" % (days, days, days)
        elif segment == 'high_balance':
            extra_where = "AND COALESCE(w.balance_available, 0) >= %s" % threshold
        else:
            extra_where = "AND COALESCE(w.balance_available, 0) <= %s" % threshold

        rows = db.execute(sa_text("""
            WITH last_bets AS (
                SELECT user_id, MAX(created_at) AS last_bet_at
                FROM (
                    SELECT user_id, created_at FROM dice_bets
                    UNION ALL
                    SELECT user_id, created_at FROM crash_bets
                    UNION ALL
                    SELECT user_id, created_at FROM global_crash_bets
                ) gaming_activity
                GROUP BY user_id
            ),
            last_deposits AS (
                SELECT user_id, MAX(created_at) AS last_deposit_at
                FROM deposits
                GROUP BY user_id
            ),
            last_withdrawals AS (
                SELECT user_id, MAX(created_at) AS last_withdrawal_at
                FROM withdrawals
                GROUP BY user_id
            )
            SELECT
                u.id AS user_id,
                u.parent_id,
                u.role,
                COALESCE(cu.is_active, u.is_active, TRUE) AS is_active,
                cu.username,
                cu.full_name,
                COALESCE(cu.email, u.email) AS email,
                cu.telegram,
                COALESCE(w.balance_available, 0) AS balance_available,
                COALESCE(w.balance_total, 0) AS balance_total,
                COALESCE(w.balance_pending, 0) AS balance_pending,
                lb.last_bet_at,
                ld.last_deposit_at,
                lw.last_withdrawal_at
            FROM users u
            LEFT JOIN c2w_users cu ON cu.user_id = u.id
            LEFT JOIN wallets w ON w.user_id = u.id
            LEFT JOIN last_bets lb ON lb.user_id = u.id
            LEFT JOIN last_deposits ld ON ld.user_id = u.id
            LEFT JOIN last_withdrawals lw ON lw.user_id = u.id
                WHERE u.id = ANY(:player_ids)
                  AND LOWER(COALESCE(u.role, '')) = 'player'
                  """ + extra_where + """
        """), {"player_ids": player_ids, "threshold": float(threshold)}).mappings().all()

        return {
            "ok": True,
            "viewer_id": viewer_id,
            "threshold": float(threshold),
            "count": len(rows),
            "items": [
                {
                    "user_id": r["user_id"],
                    "parent_id": r["parent_id"],
                    "role": r["role"],
                    "is_active": bool(r["is_active"]) if r["is_active"] is not None else True,
                    "username": r["username"],
                    "full_name": r["full_name"],
                    "email": r["email"],
                    "telegram": r["telegram"],
                    "balance_available": float(r["balance_available"] or 0),
                    "balance_total": float(r["balance_total"] or 0),
                    "balance_pending": float(r["balance_pending"] or 0),
                    "trigger_type": segment,
                    "suggested_reason": ('loss_rebate' if segment=='low_balance' else 'reactivation_bonus' if segment in ['no_bet','inactive'] else 'deposit_bonus' if segment=='no_deposit' else 'play_push'),
                    "last_bet_at": r["last_bet_at"].isoformat() if r["last_bet_at"] else None,
                    "last_deposit_at": r["last_deposit_at"].isoformat() if r["last_deposit_at"] else None,
                    "last_withdrawal_at": r["last_withdrawal_at"].isoformat() if r["last_withdrawal_at"] else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@app.post("/admin/billing/edge")
def set_billing_edge(payload: dict, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)

    parent_id = str(payload.get("parent_id") or "").strip()
    child_id = str(payload.get("child_id") or "").strip()

    if not parent_id or not child_id:
        raise HTTPException(status_code=400, detail="Missing IDs")

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE billing_edges
            SET is_active = FALSE,
                updated_at = NOW()
            WHERE child_id = :child_id
              AND parent_id <> :parent_id
              AND is_active = TRUE
        """), {
            "child_id": child_id,
            "parent_id": parent_id,
        })

        conn.execute(text("""
            INSERT INTO billing_edges (
                parent_id, child_id, billing_type,
                pph_rate, ggr_share, billing_cycle,
                sportsbook_enabled, casino_enabled, crash_enabled,
                is_active, updated_at
            ) VALUES (
                :parent_id, :child_id, :billing_type,
                :pph_rate, :ggr_share, 'monthly',
                :sportsbook_enabled, :casino_enabled, :crash_enabled,
                TRUE, NOW()
            )
            ON CONFLICT (parent_id, child_id)
            DO UPDATE SET
                billing_type = EXCLUDED.billing_type,
                pph_rate = EXCLUDED.pph_rate,
                ggr_share = EXCLUDED.ggr_share,
                billing_cycle = EXCLUDED.billing_cycle,
                sportsbook_enabled = EXCLUDED.sportsbook_enabled,
                casino_enabled = EXCLUDED.casino_enabled,
                crash_enabled = EXCLUDED.crash_enabled,
                is_active = TRUE,
                updated_at = NOW()
        """), {
            "parent_id": parent_id,
            "child_id": child_id,
            "billing_type": str(payload.get("billing_type") or "hybrid"),
            "pph_rate": float(payload.get("pph_rate") or 0),
            "ggr_share": float(payload.get("ggr_share") or 0),
            "sportsbook_enabled": bool(payload.get("sportsbook_enabled", True)),
            "casino_enabled": bool(payload.get("casino_enabled", True)),
            "crash_enabled": bool(payload.get("crash_enabled", True)),
        })

    return {"ok": True}




@app.get("/admin/agent-dashboard/{viewer_id}")
def admin_agent_dashboard(
    viewer_id: str,
    days: int = 30,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    days = max(1, min(int(days), 365))

    db = SessionLocal()
    try:
        viewer = _enforce_hierarchy_scope(db, viewer_id, viewer_id)
        tree = _get_subtree_rows(db, viewer_id)
        ids = [str(r["id"]) for r in tree if r.get("id")]
        player_ids = [str(r["id"]) for r in tree if str(r.get("role") or "").strip().lower() == "player"]

        viewer_row = db.execute(text("""
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
            WHERE id = :viewer_id
            LIMIT 1
        """), {"viewer_id": viewer_id}).mappings().first()

        wallet_row = db.execute(text("""
            SELECT
                COALESCE(SUM(balance_total), 0) AS balance_total,
                COALESCE(SUM(balance_available), 0) AS balance_available
            FROM wallets
            WHERE user_id = ANY(:ids)
        """), {"ids": ids}).mappings().first() if ids else {
            "balance_total": 0,
            "balance_available": 0,
        }

        active_players = db.execute(text("""
            WITH recent_activity AS (
                SELECT DISTINCT user_id
                FROM crash_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)

                UNION

                SELECT DISTINCT user_id
                FROM global_crash_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)

                UNION

                SELECT DISTINCT user_id
                FROM dice_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)
            )
            SELECT COUNT(*) AS active_players
            FROM recent_activity
            WHERE user_id = ANY(:player_ids)
        """), {"days": days, "player_ids": player_ids}).scalar() if player_ids else 0

        crash_ggr = db.execute(text("""
            SELECT COALESCE(SUM(amount_usd - payout), 0)
            FROM crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": ids}).scalar() if ids else 0

        global_crash_ggr = db.execute(text("""
            SELECT COALESCE(SUM(amount_usd - payout), 0)
            FROM global_crash_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": ids}).scalar() if ids else 0

        dice_ggr = db.execute(text("""
            SELECT COALESCE(SUM(amount_usd - payout), 0)
            FROM dice_bets
            WHERE user_id = ANY(:ids)
        """), {"ids": ids}).scalar() if ids else 0

        softswiss_casino_ggr = db.execute(text("""
            SELECT COALESCE(-SUM(wallet_delta), 0)
            FROM softswiss_transactions
            WHERE user_id = ANY(:ids)
              AND status = 'processed'
              AND type IN ('bet', 'win', 'rollback')
        """), {"ids": ids}).scalar() if ids else 0

        billing_history_rows = db.execute(text("""
            SELECT
                id,
                billing_mode,
                period_key,
                total_amount,
                details_json,
                created_at
            FROM billing_runs
            WHERE parent_id = :viewer_id OR child_id = :viewer_id OR agent_id = :viewer_id
            ORDER BY created_at DESC
            LIMIT 20
        """), {"viewer_id": viewer_id}).mappings().all()

        billing_edges_rows = db.execute(text("""
            SELECT
                id,
                parent_id,
                child_id,
                billing_type,
                billing_cycle,
                pph_rate,
                ggr_share,
                sportsbook_enabled,
                casino_enabled,
                crash_enabled,
                is_active,
                created_at,
                updated_at
            FROM billing_edges
            WHERE parent_id = :viewer_id
            ORDER BY updated_at DESC NULLS LAST, id DESC
        """), {"viewer_id": viewer_id}).mappings().all() if ids else []

        return {
            "ok": True,
            "viewer": {
                "id": viewer_row["id"] if viewer_row else viewer_id,
                "role": viewer_row["role"] if viewer_row else viewer.get("role"),
                "parent_id": viewer_row["parent_id"] if viewer_row else None,
                "created_by": viewer_row["created_by"] if viewer_row else None,
                "agent_code": viewer_row["agent_code"] if viewer_row else None,
                "is_active": bool(viewer_row["is_active"]) if viewer_row and viewer_row["is_active"] is not None else True,
                "created_at": viewer_row["created_at"].isoformat() if viewer_row and viewer_row["created_at"] else None,
                "billing_type": viewer_row["billing_type"] if viewer_row else None,
                "pph_rate": float(viewer_row["pph_rate"] or 0) if viewer_row else 0.0,
                "ggr_share": float(viewer_row["ggr_share"] or 0) if viewer_row else 0.0,
            },
            "summary": {
                "balance_total": float((wallet_row or {}).get("balance_total") or 0),
                "balance_available": float((wallet_row or {}).get("balance_available") or 0),
                "active_players": int(active_players or 0),
                "downline_count": max(len(ids) - 1, 0),
                "ggr": float((crash_ggr or 0) + (global_crash_ggr or 0) + (dice_ggr or 0) + (softswiss_casino_ggr or 0)),
                "ggr_breakdown": {
                    "crash": float(crash_ggr or 0),
                    "global_crash": float(global_crash_ggr or 0),
                    "dice": float(dice_ggr or 0),
                    "casino_aggregator": float(softswiss_casino_ggr or 0),
                    "sportsbook": 0.0,
                },
                "activity_window_days": days,
            },
            "hierarchy": {
                "viewer_id": viewer_id,
                "viewer_role": viewer.get("role"),
                "count": len(tree),
                "tree": tree,
            },
            "billing_config": {
                "service_pph": float(viewer_row["service_pph"] or 0) if viewer_row else 0.0,
                "service_ggr": float(viewer_row["service_ggr"] or 0) if viewer_row else 0.0,
                "originals_ggr": float(viewer_row["originals_ggr"] or 0) if viewer_row else 0.0,
                "casino_ggr": float(viewer_row["casino_ggr"] or 0) if viewer_row else 0.0,
                "live_betting_ggr": float(viewer_row["live_betting_ggr"] or 0) if viewer_row else 0.0,
                "edges": [
                    {
                        "id": r["id"],
                        "parent_id": r["parent_id"],
                        "child_id": r["child_id"],
                        "billing_type": r["billing_type"],
                        "billing_cycle": r["billing_cycle"],
                        "pph_rate": float(r["pph_rate"] or 0),
                        "ggr_share": float(r["ggr_share"] or 0),
                        "sportsbook_enabled": bool(r["sportsbook_enabled"]) if r["sportsbook_enabled"] is not None else False,
                        "casino_enabled": bool(r["casino_enabled"]) if r["casino_enabled"] is not None else False,
                        "crash_enabled": bool(r["crash_enabled"]) if r["crash_enabled"] is not None else False,
                        "is_active": bool(r["is_active"]) if r["is_active"] is not None else True,
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                    }
                    for r in billing_edges_rows
                ],
            },
            "billing_history": [
                {
                    "id": r["id"],
                    "billing_mode": r["billing_mode"],
                    "period_key": r["period_key"],
                    "total_amount": float(r["total_amount"] or 0),
                    "details_json": r["details_json"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in billing_history_rows
            ],
        }
    finally:
        db.close()



@app.post("/admin/billing/sync-edge/{child_id}")
def admin_billing_sync_edge(
    child_id: str,
    payload: dict = {},
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        child = db.execute(text("""
            SELECT id, role, parent_id
            FROM users
            WHERE id = :child_id
            LIMIT 1
        """), {"child_id": child_id}).mappings().first()

        if not child:
            raise HTTPException(status_code=404, detail="Child user not found")

        child_role = str(child["role"] or "").strip().lower()
        if child_role not in ("agent", "subagent", "master", "masteragent"):
            raise HTTPException(status_code=400, detail="Billing can only be enabled for agent-type users")

        parent_id = str(child["parent_id"] or "").strip()
        if not parent_id:
            raise HTTPException(status_code=400, detail="Child has no parent_id")

        parent = db.execute(text("""
            SELECT id, role
            FROM users
            WHERE id = :parent_id
            LIMIT 1
        """), {"parent_id": parent_id}).mappings().first()

        if not parent:
            raise HTTPException(status_code=404, detail="Parent user not found")

        billing_type = str(payload.get("billing_type") or "hybrid").strip() or "hybrid"
        pph_rate = float(payload.get("pph_rate") or 0)
        ggr_share = float(payload.get("ggr_share") or 0)
        billing_cycle = str(payload.get("billing_cycle") or "monthly").strip() or "monthly"
        sportsbook_enabled = bool(payload.get("sportsbook_enabled", True))
        casino_enabled = bool(payload.get("casino_enabled", True))
        crash_enabled = bool(payload.get("crash_enabled", True))

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE billing_edges
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE child_id = :child_id
                  AND is_active = TRUE
                  AND parent_id <> :parent_id
            """), {
                "child_id": child_id,
                "parent_id": parent_id,
            })

            conn.execute(text("""
                INSERT INTO billing_edges (
                    parent_id,
                    child_id,
                    billing_type,
                    pph_rate,
                    ggr_share,
                    billing_cycle,
                    sportsbook_enabled,
                    casino_enabled,
                    crash_enabled,
                    is_active,
                    updated_at
                ) VALUES (
                    :parent_id,
                    :child_id,
                    :billing_type,
                    :pph_rate,
                    :ggr_share,
                    :billing_cycle,
                    :sportsbook_enabled,
                    :casino_enabled,
                    :crash_enabled,
                    TRUE,
                    NOW()
                )
                ON CONFLICT (parent_id, child_id)
                DO UPDATE SET
                    billing_type = EXCLUDED.billing_type,
                    pph_rate = EXCLUDED.pph_rate,
                    ggr_share = EXCLUDED.ggr_share,
                    billing_cycle = EXCLUDED.billing_cycle,
                    sportsbook_enabled = EXCLUDED.sportsbook_enabled,
                    casino_enabled = EXCLUDED.casino_enabled,
                    crash_enabled = EXCLUDED.crash_enabled,
                    is_active = TRUE,
                    updated_at = NOW()
            """), {
                "parent_id": parent_id,
                "child_id": child_id,
                "billing_type": billing_type,
                "pph_rate": pph_rate,
                "ggr_share": ggr_share,
                "billing_cycle": billing_cycle,
                "sportsbook_enabled": sportsbook_enabled,
                "casino_enabled": casino_enabled,
                "crash_enabled": crash_enabled,
            })

        edge = db.execute(text("""
            SELECT
                id,
                parent_id,
                child_id,
                billing_type,
                billing_cycle,
                pph_rate,
                ggr_share,
                sportsbook_enabled,
                casino_enabled,
                crash_enabled,
                is_active,
                created_at,
                updated_at
            FROM billing_edges
            WHERE parent_id = :parent_id
              AND child_id = :child_id
            LIMIT 1
        """), {
            "parent_id": parent_id,
            "child_id": child_id,
        }).mappings().first()

        return {
            "ok": True,
            "message": "Billing relationship synced to current parent",
            "parent_id": parent_id,
            "child_id": child_id,
            "edge": {
                "id": edge["id"] if edge else None,
                "parent_id": edge["parent_id"] if edge else parent_id,
                "child_id": edge["child_id"] if edge else child_id,
                "billing_type": edge["billing_type"] if edge else billing_type,
                "billing_cycle": edge["billing_cycle"] if edge else billing_cycle,
                "pph_rate": float(edge["pph_rate"] or 0) if edge else pph_rate,
                "ggr_share": float(edge["ggr_share"] or 0) if edge else ggr_share,
                "sportsbook_enabled": bool(edge["sportsbook_enabled"]) if edge and edge["sportsbook_enabled"] is not None else sportsbook_enabled,
                "casino_enabled": bool(edge["casino_enabled"]) if edge and edge["casino_enabled"] is not None else casino_enabled,
                "crash_enabled": bool(edge["crash_enabled"]) if edge and edge["crash_enabled"] is not None else crash_enabled,
                "is_active": bool(edge["is_active"]) if edge and edge["is_active"] is not None else True,
                "created_at": edge["created_at"].isoformat() if edge and edge["created_at"] else None,
                "updated_at": edge["updated_at"].isoformat() if edge and edge["updated_at"] else None,
            }
        }
    finally:
        db.close()



@app.get("/admin/billing/summary")
def admin_billing_summary(
    viewer_id: str,
    period_key: str,
    days: int = 30,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    days = max(1, min(int(days), 365))

    db = SessionLocal()
    try:
        viewer = _enforce_hierarchy_scope(db, viewer_id, viewer_id)
        tree = _get_subtree_rows(db, viewer_id)
        ids = [str(r["id"]) for r in tree if r.get("id")]
        player_ids = [str(r["id"]) for r in tree if str(r.get("role") or "").strip().lower() == "player"]

        viewer_row = db.execute(text("""
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
            WHERE id = :viewer_id
            LIMIT 1
        """), {"viewer_id": viewer_id}).mappings().first()

        active_players = db.execute(text("""
            WITH recent_activity AS (
                SELECT DISTINCT user_id
                FROM crash_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)

                UNION

                SELECT DISTINCT user_id
                FROM global_crash_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)

                UNION

                SELECT DISTINCT user_id
                FROM dice_bets
                WHERE created_at >= NOW() - CAST((:days || ' days') AS interval)
            )
            SELECT COUNT(*) AS active_players
            FROM recent_activity
            WHERE user_id = ANY(:player_ids)
        """), {"days": days, "player_ids": player_ids}).scalar() if player_ids else 0

        billing_edges_rows = db.execute(text("""
            SELECT
                id,
                parent_id,
                child_id,
                billing_type,
                billing_cycle,
                pph_rate,
                ggr_share,
                sportsbook_enabled,
                casino_enabled,
                crash_enabled,
                is_active,
                created_at,
                updated_at
            FROM billing_edges
            WHERE parent_id = :viewer_id
            ORDER BY updated_at DESC NULLS LAST, id DESC
        """), {"viewer_id": viewer_id}).mappings().all() if ids else []

        runs_rows = db.execute(text("""
            SELECT
                id,
                parent_id,
                child_id,
                agent_id,
                billing_mode,
                period_key,
                player_count,
                sportsbook_ggr,
                casino_ggr,
                crash_ggr,
                pph_amount,
                ggr_amount,
                total_amount,
                details_json,
                created_at
            FROM billing_runs
            WHERE period_key = :period_key
              AND (parent_id = ANY(:ids) OR child_id = ANY(:ids) OR agent_id = ANY(:ids))
            ORDER BY created_at DESC, id DESC
        """), {"period_key": period_key, "ids": ids}).mappings().all() if ids else []

        run_map = {}
        for r in runs_rows:
            run_map[(str(r["parent_id"]), str(r["child_id"]))] = r

        edges = []
        summary_player_count = 0
        summary_pph_amount = 0.0
        summary_ggr_amount = 0.0
        summary_total_amount = 0.0
        summary_sportsbook_ggr = 0.0
        summary_casino_ggr = 0.0
        summary_crash_ggr = 0.0

        for r in billing_edges_rows:
            key = (str(r["parent_id"]), str(r["child_id"]))
            run = run_map.get(key)
            has_run = run is not None

            edge_item = {
                "id": r["id"],
                "parent_id": r["parent_id"],
                "child_id": r["child_id"],
                "billing_type": r["billing_type"],
                "billing_cycle": r["billing_cycle"],
                "pph_rate": float(r["pph_rate"] or 0),
                "ggr_share": float(r["ggr_share"] or 0),
                "sportsbook_enabled": bool(r["sportsbook_enabled"]) if r["sportsbook_enabled"] is not None else False,
                "casino_enabled": bool(r["casino_enabled"]) if r["casino_enabled"] is not None else False,
                "crash_enabled": bool(r["crash_enabled"]) if r["crash_enabled"] is not None else False,
                "is_active": bool(r["is_active"]) if r["is_active"] is not None else True,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "has_run": has_run,
                "run": None,
            }

            if run:
                edge_item["run"] = {
                    "id": run["id"],
                    "agent_id": run["agent_id"],
                    "billing_mode": run["billing_mode"],
                    "period_key": run["period_key"],
                    "player_count": int(run["player_count"] or 0),
                    "sportsbook_ggr": float(run["sportsbook_ggr"] or 0),
                    "casino_ggr": float(run["casino_ggr"] or 0),
                    "crash_ggr": float(run["crash_ggr"] or 0),
                    "originals_ggr": float(run["crash_ggr"] or 0),
                    "pph_amount": float(run["pph_amount"] or 0),
                    "ggr_amount": float(run["ggr_amount"] or 0),
                    "total_amount": float(run["total_amount"] or 0),
                    "details_json": run["details_json"],
                    "created_at": run["created_at"].isoformat() if run["created_at"] else None,
                }

                summary_player_count += int(run["player_count"] or 0)
                summary_pph_amount += float(run["pph_amount"] or 0)
                summary_ggr_amount += float(run["ggr_amount"] or 0)
                summary_total_amount += float(run["total_amount"] or 0)
                summary_sportsbook_ggr += float(run["sportsbook_ggr"] or 0)
                summary_casino_ggr += float(run["casino_ggr"] or 0)
                summary_crash_ggr += float(run["crash_ggr"] or 0)

            edges.append(edge_item)

        active_edges = [e for e in edges if e.get("is_active")]
        inactive_edges = [e for e in edges if not e.get("is_active")]

        return {
            "ok": True,
            "viewer": {
                "id": viewer_row["id"] if viewer_row else viewer_id,
                "role": viewer_row["role"] if viewer_row else viewer.get("role"),
                "parent_id": viewer_row["parent_id"] if viewer_row else None,
                "created_by": viewer_row["created_by"] if viewer_row else None,
                "agent_code": viewer_row["agent_code"] if viewer_row else None,
                "is_active": bool(viewer_row["is_active"]) if viewer_row and viewer_row["is_active"] is not None else True,
                "created_at": viewer_row["created_at"].isoformat() if viewer_row and viewer_row["created_at"] else None,
                "billing_type": viewer_row["billing_type"] if viewer_row else None,
                "pph_rate": float(viewer_row["pph_rate"] or 0) if viewer_row else 0.0,
                "ggr_share": float(viewer_row["ggr_share"] or 0) if viewer_row else 0.0,
            },
            "period_key": period_key,
            "summary": {
                "edge_count": len(edges),
                "active_relationship_count": len(active_edges),
                "inactive_relationship_count": len(inactive_edges),
                "run_count": sum(1 for e in edges if e["has_run"]),
                "has_any_run": any(e["has_run"] for e in edges),
                "player_count": int(summary_player_count),
                "active_players": int(active_players or 0),
                "pph_amount": round(summary_pph_amount, 2),
                "ggr_amount": round(summary_ggr_amount, 2),
                "total_amount": round(summary_total_amount, 2),
                "sportsbook_ggr": round(summary_sportsbook_ggr, 2),
                "casino_ggr": round(summary_casino_ggr, 2),
                "crash_ggr": round(summary_crash_ggr, 2),
                "originals_ggr": round(summary_crash_ggr, 2),
                "activity_window_days": days,
            },
            "edges": edges,
            "active_relationships": active_edges,
            "inactive_relationships": inactive_edges,
        }
    finally:
        db.close()


@app.post("/admin/billing/run-global")
def admin_billing_run_global(payload: dict = {}, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    period_key = str(payload.get("period_key") or datetime.now(timezone.utc).strftime("%Y-%m"))
    from app.billing_runner import run_global_billing
    return {"ok": True, "runs": run_global_billing(period_key)}
# ==========================
# DASHBOARD SUMMARY (AUTO)
# ==========================
from datetime import datetime, timedelta, timezone
from sqlalchemy import func

ORIGINALS_GGR_SOURCES = [
    {"key": "dice", "model": DiceBet},
    {"key": "crash", "model": CrashBet},
    {"key": "global_crash", "model": GlobalCrashBet},
]

def _utc_now():
    return datetime.now(timezone.utc)

def _today_bounds_utc():
    now = _utc_now()
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end

def _last_24h_start_utc():
    return _utc_now() - timedelta(hours=24)

def _scoped_user_ids(db, viewer_id: str | None):
    if not viewer_id:
        return None
    _enforce_hierarchy_scope(db, viewer_id, viewer_id)
    tree = _get_subtree_rows(db, viewer_id)
    return [str(r["id"]) for r in tree]

def _sum(x): return round(float(x or 0), 2)
def _count(x): return int(x or 0)

def _dashboard_summary(db, scoped_ids=None, viewer_id=None):
    today_start, today_end = _today_bounds_utc()
    last_24h = _last_24h_start_utc()

    # deposits (transactions)
    tx = db.query(Transaction).filter(Transaction.type=="deposit")
    if scoped_ids: tx = tx.filter(Transaction.user_id.in_(scoped_ids))

    dep_today = _sum(tx.filter(Transaction.created_at>=today_start, Transaction.created_at<today_end).with_entities(func.sum(Transaction.amount)).scalar())
    dep_count = _count(tx.filter(Transaction.created_at>=today_start, Transaction.created_at<today_end).with_entities(func.count()).scalar())

    # withdrawals
    wd = db.query(Withdrawal)
    if scoped_ids: wd = wd.filter(Withdrawal.user_id.in_(scoped_ids))

    requested = _sum(wd.filter(Withdrawal.status=="requested").with_entities(func.sum(Withdrawal.amount_usd)).scalar())
    approved  = _sum(wd.filter(Withdrawal.status=="approved").with_entities(func.sum(Withdrawal.amount_usd)).scalar())
    sent      = _sum(wd.filter(Withdrawal.status=="sent").with_entities(func.sum(Withdrawal.amount_usd)).scalar())

    completed_today = _sum(
        wd.filter(
            Withdrawal.status=="completed",
            Withdrawal.completed_at>=today_start,
            Withdrawal.completed_at<today_end
        ).with_entities(func.sum(Withdrawal.amount_usd)).scalar()
    )

    # GGR originals
    breakdown = {}
    originals_total = 0

    for s in ORIGINALS_GGR_SOURCES:
        m = s["model"]
        q = db.query(func.coalesce(func.sum(m.amount_usd),0), func.coalesce(func.sum(m.payout),0))
        if scoped_ids: q = q.filter(m.user_id.in_(scoped_ids))
        a,p = q.one()
        g = round(float(a)-float(p),2)
        breakdown[s["key"]] = g
        originals_total += g

    originals_total = round(originals_total,2)

    # players active
    active = set()
    for q in [
        db.query(Transaction.user_id).filter(Transaction.created_at>=last_24h),
        db.query(Withdrawal.user_id).filter(Withdrawal.created_at>=last_24h),
    ]:
        if scoped_ids: q = q.filter(q.column_descriptions[0]["entity"].user_id.in_(scoped_ids))
        active.update([x[0] for x in q.distinct().all() if x[0]])

    vendor_casino_total = db.execute(text("""
        SELECT COALESCE(-SUM(wallet_delta), 0)
        FROM softswiss_transactions
        WHERE status = 'processed'
          AND type IN ('bet', 'win', 'rollback')
    """)).scalar() or 0
    vendor_casino_total = float(vendor_casino_total or 0)

    return {
        "deposits": {
            "today": dep_today,
            "count": dep_count
        },
        "withdrawals": {
            "requested": requested,
            "approved": approved,
            "sent": sent,
            "completed_today": completed_today
        },
        "risk": {
            "open_withdrawals": round(requested+approved+sent,2)
        },
        "cashflow": {
            "net_today": round(dep_today - completed_today,2)
        },
        "ggr": {
            "total_platform_ggr": originals_total + vendor_casino_total,
            "originals_total": originals_total,
            "sportsbook_total": 0.0,
            "vendor_casino_total": vendor_casino_total,
            "originals_breakdown": breakdown
        },
        "player_performance": {"top_winners": locals().get("top_winners", []), "top_losers": locals().get("top_losers", [])},
            "players": {
            "active_24h": len(active)
        }
    }

@app.get("/admin/dashboard/summary")
def admin_dashboard_summary(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        return _dashboard_summary(db)
    finally:
        db.close()

@app.get("/agent/dashboard/summary")
def agent_dashboard_summary(viewer_id: str, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        ids = _scoped_user_ids(db, viewer_id)
        return _dashboard_summary(db, ids, viewer_id)
    finally:
        db.close()


# ==========================
# DASHBOARD RANGE PATCH
# ==========================

def _range_bounds(range_key: str):
    now = _utc_now()

    if range_key == "7d":
        return now - timedelta(days=7), now
    if range_key == "30d":
        return now - timedelta(days=30), now
    if range_key == "90d":
        return now - timedelta(days=90), now
    if range_key == "180d":
        return now - timedelta(days=180), now

    # default today
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _dashboard_summary_range(db, scoped_ids=None, viewer_id=None, range_key="today"):
    start, end = _range_bounds(range_key)

    # deposits
    tx = db.query(Transaction).filter(Transaction.type=="deposit")
    if scoped_ids: tx = tx.filter(Transaction.user_id.in_(scoped_ids))

    deposits = _sum(
        tx.filter(Transaction.created_at>=start, Transaction.created_at<end)
        .with_entities(func.sum(Transaction.amount)).scalar()
    )

    deposit_count = _count(
        tx.filter(Transaction.created_at>=start, Transaction.created_at<end)
        .with_entities(func.count()).scalar()
    )

    # withdrawals
    wd = db.query(Withdrawal)
    if scoped_ids: wd = wd.filter(Withdrawal.user_id.in_(scoped_ids))

    completed = _sum(
        wd.filter(
            Withdrawal.status=="completed",
            Withdrawal.completed_at>=start,
            Withdrawal.completed_at<end
        ).with_entities(func.sum(Withdrawal.amount_usd)).scalar()
    )

    # GGR
    breakdown = {}
    originals_total = 0

    for s in ORIGINALS_GGR_SOURCES:
        m = s["model"]
        q = db.query(func.coalesce(func.sum(m.amount_usd),0), func.coalesce(func.sum(m.payout),0))
        if scoped_ids: q = q.filter(m.user_id.in_(scoped_ids))
        q = q.filter(m.created_at>=start, m.created_at<end)

        a,p = q.one()
        g = round(float(a)-float(p),2)
        breakdown[s["key"]] = g
        originals_total += g

    originals_total = round(originals_total,2)

    # players active
    active = set()
    for q in [
        db.query(Transaction.user_id).filter(Transaction.created_at>=start),
        db.query(Withdrawal.user_id).filter(Withdrawal.created_at>=start),
    ]:
        if scoped_ids: q = q.filter(q.column_descriptions[0]["entity"].user_id.in_(scoped_ids))
        active.update([x[0] for x in q.distinct().all() if x[0]])

    # risk stays live (no range)
    wd_live = db.query(Withdrawal)
    if scoped_ids: wd_live = wd_live.filter(Withdrawal.user_id.in_(scoped_ids))

    requested = _sum(wd_live.filter(Withdrawal.status=="requested").with_entities(func.sum(Withdrawal.amount_usd)).scalar())
    approved  = _sum(wd_live.filter(Withdrawal.status=="approved").with_entities(func.sum(Withdrawal.amount_usd)).scalar())
    sent      = _sum(wd_live.filter(Withdrawal.status=="sent").with_entities(func.sum(Withdrawal.amount_usd)).scalar())


    # player performance (global platform-ready; originals active now)
    player_map = {}

    def _accumulate_player(user_id, wager, payout, vertical):
        if not user_id:
            return
        uid = str(user_id)
        if uid not in player_map:
            player_map[uid] = {
                "user_id": uid,
                "wagered": 0.0,
                "net": 0.0,
                "breakdown": {
                    "originals": 0.0,
                    "sportsbook": 0.0,
                    "vendor_casino": 0.0,
                },
            }
        wager = float(wager or 0.0)
        payout = float(payout or 0.0)
        net = round(payout - wager, 2)
        player_map[uid]["wagered"] = round(player_map[uid]["wagered"] + wager, 2)
        player_map[uid]["net"] = round(player_map[uid]["net"] + net, 2)
        player_map[uid]["breakdown"][vertical] = round(player_map[uid]["breakdown"][vertical] + net, 2)

    q = db.query(DiceBet).filter(DiceBet.created_at >= start, DiceBet.created_at < end)
    if scoped_ids:
        q = q.filter(DiceBet.user_id.in_(scoped_ids))
    for r in q.all():
        _accumulate_player(r.user_id, r.amount_usd, r.payout, "originals")

    q = db.query(CrashBet).filter(CrashBet.created_at >= start, CrashBet.created_at < end)
    if scoped_ids:
        q = q.filter(CrashBet.user_id.in_(scoped_ids))
    for r in q.all():
        _accumulate_player(r.user_id, r.amount_usd, r.payout, "originals")

    q = db.query(GlobalCrashBet).filter(GlobalCrashBet.created_at >= start, GlobalCrashBet.created_at < end)
    if scoped_ids:
        q = q.filter(GlobalCrashBet.user_id.in_(scoped_ids))
    for r in q.all():
        _accumulate_player(r.user_id, r.amount_usd, r.payout, "originals")

    players_perf = list(player_map.values())
    players_perf.sort(key=lambda x: x["net"], reverse=True)
    top_winners = [x for x in players_perf if x["net"] > 0][:5]
    top_losers = sorted([x for x in players_perf if x["net"] < 0], key=lambda x: x["net"])[:5]

    return {
        "range": range_key,
        "deposits": {
            "total": deposits,
            "count": deposit_count
        },
        "withdrawals": {
            "completed": completed
        },
        "risk": {
            "open_withdrawals": round(requested+approved+sent,2)
        },
        "cashflow": {
            "net": round(deposits - completed,2)
        },
        "ggr": {
            "total_platform_ggr": originals_total,
            "originals_total": originals_total,
            "sportsbook_total": 0.0,
            "vendor_casino_total": 0.0,
            "originals_breakdown": breakdown
        },
        "player_performance": {"top_winners": locals().get("top_winners", []), "top_losers": locals().get("top_losers", [])},
            "players": {
            "active": len(active)
        }
    }


@app.get("/admin/dashboard/summary-range")
def admin_dashboard_summary_range(
    range: str = "today",
    viewer_id: str | None = None,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        scoped_ids = _scoped_user_ids(db, viewer_id) if viewer_id else None
        return _dashboard_summary_range(db, scoped_ids, viewer_id, range)
    finally:
        db.close()


@app.get("/agent/dashboard/summary-range")
def agent_dashboard_summary_range(
    viewer_id: str,
    range: str = "today",
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")
):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        ids = _scoped_user_ids(db, viewer_id)
        return _dashboard_summary_range(db, ids, viewer_id, range)
    finally:
        db.close()


from pydantic import BaseModel, EmailStr
from app.auth_routes import hash_password

class AdminCreateUserBody(BaseModel):
    viewer_id: str
    parent_id: str
    role: str
    email: EmailStr
    username: str
    password: str
    agent_code: str | None = None
    is_active: bool = True

@app.post("/admin/users/create")
def admin_create_user(body: AdminCreateUserBody, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        viewer = _get_user_hierarchy_row(db, body.viewer_id)
        if not viewer:
            raise HTTPException(status_code=404, detail="Viewer not found")

        viewer_role = _normalize_role(viewer.get("role"))
        role = _normalize_role(body.role)

        if not _can_create_child_role(viewer_role, role):
            raise HTTPException(status_code=403, detail="Role creation not allowed")

        parent = _get_user_hierarchy_row(db, body.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent not found")

        if not _is_descendant_or_self(db, body.viewer_id, body.parent_id):
            raise HTTPException(status_code=403, detail="Parent outside your hierarchy")

        user_id = body.username.strip()

        existing_auth = db.execute(sa_text("""
            SELECT 1 FROM c2w_users WHERE user_id = :uid
        """), {"uid": user_id}).fetchone()

        existing_user = db.execute(sa_text("""
            SELECT 1 FROM users WHERE id = :uid
        """), {"uid": user_id}).fetchone()

        if existing_auth or existing_user:
            raise HTTPException(status_code=400, detail="User already exists")

        password_hash = hash_password(body.password)

        db.execute(sa_text("""
            INSERT INTO c2w_users (user_id, email, username, password_hash, is_active)
            VALUES (:user_id, :email, :username, :password_hash, :is_active)
        """), {
            "user_id": user_id,
            "email": body.email.lower(),
            "username": body.username,
            "password_hash": password_hash,
            "is_active": body.is_active,
        })

        db.execute(sa_text("""
            INSERT INTO users (
                id, role, parent_id, created_by, agent_code,
                is_active, billing_type,
                pph_rate, ggr_share,
                service_pph, service_ggr,
                originals_ggr, casino_ggr, live_betting_ggr
            )
            VALUES (
                :id, :role, :parent_id, :created_by, :agent_code,
                :is_active, 'ggr',
                0, 0,
                0, 0,
                0, 0, 0
            )
        """), {
            "id": user_id,
            "role": role,
            "parent_id": body.parent_id,
            "created_by": body.viewer_id,
            "agent_code": body.agent_code,
            "is_active": body.is_active,
        })

        if role == "player":
            db.execute(sa_text("""
                INSERT INTO wallets (user_id, balance_total, balance_pending, balance_available)
                VALUES (:user_id, 0, 0, 0)
            """), {"user_id": user_id})

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "role": role
        }

    finally:
        db.close()


class AdminUpdateUserMetadataBody(BaseModel):
    parent_id: str | None = None
    agent_code: str | None = None
    created_by: str | None = None
    updated_by: str | None = None


@app.post("/admin/users/{user_id}/disable")
def admin_disable_user(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        body = {}
        try:
            body = {}
        except Exception:
            body = {}

        actor_id = "system"

        row = _get_user_hierarchy_row(db, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        db.execute(sa_text("""
            UPDATE users
            SET is_active = FALSE,
                updated_at = NOW(),
                updated_by = :updated_by
            WHERE id = :user_id
        """), {
            "user_id": user_id,
            "updated_by": actor_id,
        })

        db.execute(sa_text("""
            UPDATE c2w_users
            SET is_active = FALSE
            WHERE user_id = :user_id
        """), {
            "user_id": user_id,
        })

        db.commit()
        return {"ok": True, "user_id": user_id, "is_active": False}
    finally:
        db.close()


@app.post("/admin/users/{user_id}/enable")
def admin_enable_user(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        actor_id = "system"

        row = _get_user_hierarchy_row(db, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        db.execute(sa_text("""
            UPDATE users
            SET is_active = TRUE,
                updated_at = NOW(),
                updated_by = :updated_by
            WHERE id = :user_id
        """), {
            "user_id": user_id,
            "updated_by": actor_id,
        })

        db.execute(sa_text("""
            UPDATE c2w_users
            SET is_active = TRUE
            WHERE user_id = :user_id
        """), {
            "user_id": user_id,
        })

        db.commit()
        return {"ok": True, "user_id": user_id, "is_active": True}
    finally:
        db.close()


@app.post("/admin/users/{user_id}/update-metadata")
def admin_update_user_metadata(
    user_id: str,
    body: AdminUpdateUserMetadataBody,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        target = _get_user_hierarchy_row(db, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        actor_id = str(body.updated_by or "").strip() or "system"
        actor = _get_user_hierarchy_row(db, actor_id)
        if not actor:
            raise HTTPException(status_code=404, detail="Updated_by user not found")

        _enforce_hierarchy_scope(db, actor_id, user_id)

        updates = []
        params = {
            "user_id": user_id,
            "updated_by": actor_id,
        }

        if body.parent_id is not None:
            new_parent = str(body.parent_id).strip() or None
            if new_parent:
                parent = _get_user_hierarchy_row(db, new_parent)
                if not parent:
                    raise HTTPException(status_code=404, detail="Parent not found")
                _enforce_hierarchy_scope(db, actor_id, new_parent)
            updates += [
                "parent_id = :parent_id",
                "last_parent_change_at = NOW()",
                "last_parent_change_by = :updated_by",
            ]
            params["parent_id"] = new_parent

        if body.agent_code is not None:
            new_agent_code = str(body.agent_code).strip() or None
            updates += [
                "agent_code = :agent_code",
                "last_agent_code_change_at = NOW()",
                "last_agent_code_change_by = :updated_by",
            ]
            params["agent_code"] = new_agent_code

        if body.created_by is not None:
            new_created_by = str(body.created_by).strip() or None
            if new_created_by:
                creator = _get_user_hierarchy_row(db, new_created_by)
                if not creator:
                    raise HTTPException(status_code=404, detail="Created_by user not found")
            updates += [
                "created_by = :created_by",
                "last_created_by_change_at = NOW()",
                "last_created_by_change_by = :updated_by",
            ]
            params["created_by"] = new_created_by

        if not updates:
            raise HTTPException(status_code=400, detail="No metadata fields provided")

        updates += [
            "updated_at = NOW()",
            "updated_by = :updated_by",
        ]

        sql = f"""
            UPDATE users
            SET {", ".join(updates)}
            WHERE id = :user_id
        """

        db.execute(sa_text(sql), params)
        db.commit()

        return {"ok": True, "user_id": user_id}
    finally:
        db.close()


@app.post("/admin/users/{user_id}/update-contact")
async def admin_update_user_contact(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)
        target = _get_user_hierarchy_row(db, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        body = await request.json()
        full_name = str(body.get("full_name", "") or "").strip() or None
        telegram = str(body.get("telegram", "") or "").strip() or None
        phone = str(body.get("phone", "") or "").strip() or None
        notes = str(body.get("notes", "") or "").strip() or None
        actor_id = str(body.get("updated_by", "") or "").strip() or "admin"

        db.execute(sa_text("""
            INSERT INTO c2w_users (
                user_id,
                email,
                username,
                password_hash,
                is_active,
                created_at,
                full_name,
                telegram,
                phone,
                notes
            )
            SELECT
                u.id,
                COALESCE(cu.email, u.email, CASE WHEN POSITION('@' IN u.id) > 0 THEN u.id ELSE NULL END),
                COALESCE(cu.username, NULLIF(split_part(u.id, '@', 1), ''), u.id),
                COALESCE(cu.password_hash, u.password_hash, ''),
                COALESCE(cu.is_active, u.is_active, TRUE),
                COALESCE(cu.created_at, NOW()),
                :full_name,
                :telegram,
                :phone,
                :notes
            FROM users u
            LEFT JOIN c2w_users cu ON cu.user_id = u.id
            WHERE u.id = :user_id
            ON CONFLICT (user_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                telegram = EXCLUDED.telegram,
                phone = EXCLUDED.phone,
                notes = EXCLUDED.notes
        """), {
            "user_id": user_id,
            "full_name": full_name,
            "telegram": telegram,
            "phone": phone,
            "notes": notes,
        })

        db.execute(sa_text("""
            UPDATE users
            SET updated_at = NOW(),
                updated_by = :updated_by
            WHERE id = :user_id
        """), {
            "user_id": user_id,
            "updated_by": actor_id,
        })

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "full_name": full_name,
            "telegram": telegram,
            "phone": phone,
            "notes": notes,
        }
    finally:
        db.close()


from pydantic import BaseModel

class BillingEdgeUpdate(BaseModel):
    parent_id: str
    child_id: str
    billing_type: str | None = None
    billing_cycle: str | None = None
    pph_rate: float | None = None
    ggr_share: float | None = None
    sportsbook_enabled: bool | None = None
    casino_enabled: bool | None = None
    crash_enabled: bool | None = None

@app.post("/admin/billing/edge/update")
def update_billing_edge(payload: BillingEdgeUpdate, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = SessionLocal()
    try:
        edge = db.execute(text("""
            SELECT * FROM billing_edges
            WHERE parent_id = :parent_id
              AND child_id = :child_id
              AND is_active = TRUE
            ORDER BY id DESC
            LIMIT 1
        """), payload.dict()).mappings().first()

        if not edge:
            return {"ok": False, "error": "Active edge not found"}

        update_fields = {k:v for k,v in payload.dict().items() if v is not None and k not in ["parent_id","child_id"]}

        if not update_fields:
            return {"ok": False, "error": "No fields to update"}

        set_clause = ", ".join([f"{k} = :{k}" for k in update_fields.keys()])

        db.execute(text(f"""
            UPDATE billing_edges
            SET {set_clause}, updated_at = NOW()
            WHERE id = :id
        """), {**update_fields, "id": edge["id"]})

        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ==========================
# PUBLIC / ADMIN BRAND CMS API
# ==========================

def _brand_row_to_dict(r):
    if not r:
        return None
    return {
        "id": r["id"],
        "owner_user_id": r["owner_user_id"],
        "brand_name": r["brand_name"],
        "domain": r["domain"],
        "logo_url": r["logo_url"],
        "favicon_url": r["favicon_url"],
        "primary_color": r["primary_color"],
        "secondary_color": r["secondary_color"],
        "support_email": r["support_email"],
        "support_telegram": r["support_telegram"],
        "is_active": bool(r["is_active"]) if r["is_active"] is not None else True,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "home_banners_json": r["home_banners_json"] or [],
        "casino_banners_json": r["casino_banners_json"] or [],
        "casino_lobby_json": r["casino_lobby_json"] or [],
        "promotion_cards_json": r["promotion_cards_json"] or [],
        "promotion_faq_json": r["promotion_faq_json"] or [],
        "theme": r["theme"] or "default",
    }


@app.get("/api/public/brand-by-host")
def api_public_brand_by_host(host: str = ""):
    clean_host = str(host or "").strip().lower().split(":")[0]
    if clean_host.startswith("www."):
        clean_host = clean_host[4:]

    db = SessionLocal()
    try:
        row = db.execute(text("""
            SELECT *
            FROM brand_domains
            WHERE is_active = TRUE
              AND LOWER(domain) IN (:host, :www_host)
            ORDER BY id DESC
            LIMIT 1
        """), {
            "host": clean_host,
            "www_host": "www." + clean_host,
        }).mappings().first()

        if not row:
            row = db.execute(text("""
                SELECT *
                FROM brand_domains
                WHERE is_active = TRUE
                ORDER BY id ASC
                LIMIT 1
            """)).mappings().first()

        return {"ok": True, "brand": _brand_row_to_dict(row)}
    finally:
        db.close()


@app.get("/api/admin/brands")
def api_admin_brands(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT *
            FROM brand_domains
            ORDER BY id DESC
        """)).mappings().all()

        return {"ok": True, "brands": [_brand_row_to_dict(r) for r in rows]}
    finally:
        db.close()


@app.post("/api/admin/brands")
async def api_admin_create_brand(request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()

    db = SessionLocal()
    try:
        row = db.execute(text("""
            INSERT INTO brand_domains (
                owner_user_id, brand_name, domain,
                logo_url, favicon_url,
                primary_color, secondary_color,
                support_email, support_telegram,
                is_active,
                home_banners_json, casino_banners_json, casino_lobby_json,
                promotion_cards_json, promotion_faq_json,
                theme
            )
            VALUES (
                :owner_user_id, :brand_name, :domain,
                :logo_url, :favicon_url,
                :primary_color, :secondary_color,
                :support_email, :support_telegram,
                :is_active,
                CAST(:home_banners_json AS jsonb),
                CAST(:casino_banners_json AS jsonb),
                CAST(:casino_lobby_json AS jsonb),
                CAST(:promotion_cards_json AS jsonb),
                CAST(:promotion_faq_json AS jsonb),
                :theme
            )
            RETURNING *
        """), {
            "owner_user_id": str(body.get("owner_user_id") or "").strip(),
            "brand_name": str(body.get("brand_name") or "").strip(),
            "domain": str(body.get("domain") or "").strip().lower(),
            "logo_url": body.get("logo_url"),
            "favicon_url": body.get("favicon_url"),
            "primary_color": body.get("primary_color"),
            "secondary_color": body.get("secondary_color"),
            "support_email": body.get("support_email"),
            "support_telegram": body.get("support_telegram"),
            "is_active": bool(body.get("is_active", True)),
            "home_banners_json": json.dumps(body.get("home_banners_json") or []),
            "casino_banners_json": json.dumps(body.get("casino_banners_json") or []),
            "casino_lobby_json": json.dumps(body.get("casino_lobby_json") or []),
            "promotion_cards_json": json.dumps(body.get("promotion_cards_json") or []),
            "promotion_faq_json": json.dumps(body.get("promotion_faq_json") or []),
            "theme": str(body.get("theme") or "default"),
        }).mappings().first()

        db.commit()
        return {"ok": True, "brand": _brand_row_to_dict(row)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@app.post("/api/admin/brands/update/{brand_id}")
async def api_admin_update_brand(brand_id: int, request: Request, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _require_admin(x_admin_key)
    body = await request.json()

    allowed = {
        "brand_name", "domain", "logo_url", "favicon_url",
        "primary_color", "secondary_color",
        "support_email", "support_telegram",
        "is_active", "theme",
    }
    json_fields = {
        "home_banners_json", "casino_banners_json", "casino_lobby_json",
        "promotion_cards_json", "promotion_faq_json",
    }

    updates = {}
    for k, v in body.items():
        if k in allowed:
            updates[k] = v
        elif k in json_fields:
            updates[k] = json.dumps(v or [])

    if not updates:
        return {"ok": False, "detail": "No valid fields to update"}

    set_parts = []
    params = {"brand_id": brand_id}

    for k, v in updates.items():
        if k in json_fields:
            set_parts.append(f"{k} = CAST(:{k} AS jsonb)")
        else:
            set_parts.append(f"{k} = :{k}")
        params[k] = v

    db = SessionLocal()
    try:
        row = db.execute(text(f"""
            UPDATE brand_domains
            SET {", ".join(set_parts)}
            WHERE id = :brand_id
            RETURNING *
        """), params).mappings().first()

        db.commit()

        if not row:
            raise HTTPException(status_code=404, detail="Brand not found")

        return {"ok": True, "brand": _brand_row_to_dict(row)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@app.post("/admin/users/{user_id}/wallet-adjust")
async def admin_wallet_adjust(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        body = await request.json()

        amount = float(body.get("amount", 0) or 0)
        reason = str(body.get("reason", "") or "").strip()
        action = str(body.get("action", "") or "").strip().lower()
        actor_id = str(body.get("actor_id", "") or "").strip() or "admin"

        if amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")

        if action not in ("credit", "debit"):
            action = "credit"

        wallet = (
            db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .one()
        )

        if action == "credit":
            wallet.balance_total = _round2(wallet.balance_total + amount)
            wallet.balance_available = _round2(wallet.balance_available + amount)
            tx_amount = float(amount)
            tx_type = "manual_credit"
        else:
            if float(wallet.balance_available) < amount:
                raise HTTPException(status_code=400, detail="insufficient available balance")

            wallet.balance_total = _round2(wallet.balance_total - amount)
            wallet.balance_available = _round2(wallet.balance_available - amount)
            tx_amount = -float(amount)
            tx_type = "manual_debit"

        tx = Transaction(
            user_id=user_id,
            type=tx_type,
            amount=tx_amount,
            balance_after=float(wallet.balance_total),
            reference=f"{reason}:{actor_id}" if reason else f"manual:{actor_id}",
        )
        db.add(tx)

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "action": action,
            "amount": amount,
            "balance_total": wallet.balance_total,
            "balance_available": wallet.balance_available,
        }

    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@app.post("/admin/users/{user_id}/reset-password")
async def admin_reset_user_password(
    user_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _require_admin(x_admin_key)

    db = SessionLocal()
    try:
        _get_or_create_user_and_wallet(db, user_id)

        body = await request.json()
        new_password = str(body.get("new_password", "") or "").strip()

        if len(new_password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

        password_hash = hash_password(new_password)

        row = db.execute(sa_text("""
            SELECT 1 FROM c2w_users WHERE user_id = :user_id
        """), {"user_id": user_id}).fetchone()

        if row:
            db.execute(sa_text("""
                UPDATE c2w_users
                SET password_hash = :password_hash
                WHERE user_id = :user_id
            """), {
                "user_id": user_id,
                "password_hash": password_hash,
            })
        else:
            db.execute(sa_text("""
                INSERT INTO c2w_users (user_id, email, username, password_hash, is_active)
                VALUES (
                    :user_id,
                    CASE WHEN POSITION('@' IN :user_id) > 0 THEN :user_id ELSE NULL END,
                    NULLIF(split_part(:user_id, '@', 1), ''),
                    :password_hash,
                    TRUE
                )
            """), {
                "user_id": user_id,
                "password_hash": password_hash,
            })

        db.execute(sa_text("""
            UPDATE users
            SET updated_at = NOW(),
                updated_by = 'admin'
            WHERE id = :user_id
        """), {"user_id": user_id})

        db.commit()

        return {
            "ok": True,
            "user_id": user_id,
            "message": "Password reset successfully",
        }
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()

