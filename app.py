"""
Бэкенд Telegram Mini App «Дыхание».

Умеет:
- проверять подпись initData (HMAC-SHA256 по токену бота);
- создавать счёт Telegram Stars (Bot API createInvoiceLink, currency=XTR);
- принимать вебхук: successful_payment -> Premium; /start ref_<код> -> привязка реферала;
- партнёрка: 10% от оплаты реферала зачисляются пригласившему;
- хранить всё в Postgres (DATABASE_URL) или SQLite (локально, DB_PATH).

Переменные окружения:
- BOT_TOKEN       — токен бота от @BotFather (обязательно)
- WEBHOOK_SECRET  — секрет заголовка вебхука (рекомендуется)
- DATABASE_URL    — строка подключения Postgres (Neon и т.п.). Если нет — SQLite.
- DB_PATH         — путь к SQLite при отсутствии DATABASE_URL (по умолчанию premium.db)
- ALLOW_ORIGIN    — origin мини-аппа для CORS
- REFERRAL_PERCENT— процент партнёру (по умолчанию 10)
"""
import os
import hmac
import json
import time
import hashlib
import secrets
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "premium.db")
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
REFERRAL_PERCENT = int(os.environ.get("REFERRAL_PERCENT", "10"))

USE_PG = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

PLANS = {
    "week": {"stars": 99, "days": 7, "title": "Premium на неделю"},
    "month": {"stars": 249, "days": 30, "title": "Premium на месяц"},
    "year": {"stars": 1490, "days": 365, "title": "Premium на год"},
}

app = FastAPI(title="Breathe TMA backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOW_ORIGIN] if ALLOW_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- база (Postgres или SQLite) ----------
def _q(sql: str) -> str:
    return sql.replace("?", "%s") if USE_PG else sql


def db_conn():
    if USE_PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=()):
    with db_conn() as c:
        cur = c.execute(_q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql, params=()):
    with db_conn() as c:
        c.execute(_q(sql), params)
        c.commit()


def init_db():
    stmts = [
        """CREATE TABLE IF NOT EXISTS users (
               user_id BIGINT PRIMARY KEY,
               username TEXT, first_name TEXT,
               ref_code TEXT UNIQUE, referred_by BIGINT,
               wallet TEXT, created_at BIGINT)""",
        """CREATE TABLE IF NOT EXISTS subscriptions (
               user_id BIGINT PRIMARY KEY, plan_id TEXT,
               expires_at BIGINT, updated_at BIGINT)""",
        """CREATE TABLE IF NOT EXISTS payments (
               id TEXT PRIMARY KEY, user_id BIGINT, plan_id TEXT,
               stars INTEGER, created_at BIGINT)""",
        """CREATE TABLE IF NOT EXISTS referral_earnings (
               id TEXT PRIMARY KEY, referrer_id BIGINT, buyer_id BIGINT,
               stars INTEGER, percent INTEGER, created_at BIGINT, status TEXT)""",
        """CREATE TABLE IF NOT EXISTS payouts (
               id TEXT PRIMARY KEY, partner_id BIGINT, stars INTEGER,
               wallet TEXT, status TEXT, created_at BIGINT)""",
    ]
    with db_conn() as c:
        for s in stmts:
            c.execute(s)
        c.commit()


init_db()


def now() -> int:
    return int(time.time())


def gen_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")


# ---------- пользователи и подписки ----------
def ensure_user(user_id: int, username=None, first_name=None):
    if not user_id:
        return None
    rows = query("SELECT * FROM users WHERE user_id=?", (user_id,))
    if rows:
        return rows[0]
    for _ in range(5):
        try:
            execute(
                "INSERT INTO users(user_id, username, first_name, ref_code, created_at) VALUES(?,?,?,?,?)",
                (user_id, username, first_name, gen_code(), now()),
            )
            return query("SELECT * FROM users WHERE user_id=?", (user_id,))[0]
        except Exception:
            continue
    return query("SELECT * FROM users WHERE user_id=?", (user_id,))


def set_referral(user_id: int, code: str) -> bool:
    """Привязывает пригласившего, если у пользователя ещё нет реферера."""
    if not code or not user_id:
        return False
    rows = query("SELECT user_id FROM users WHERE ref_code=?", (code,))
    if not rows:
        return False
    referrer = rows[0]["user_id"]
    if referrer == user_id:
        return False
    me = ensure_user(user_id)
    if not me or me.get("referred_by"):
        return False
    execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer, user_id))
    return True


def set_premium(user_id: int, plan_id: str, days: int):
    ts = now()
    rows = query("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,))
    base = max(ts, rows[0]["expires_at"] if rows else ts)
    expires = base + days * 86400
    execute(
        """INSERT INTO subscriptions(user_id, plan_id, expires_at, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             plan_id=excluded.plan_id, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
        (user_id, plan_id, expires, ts),
    )
    return expires


def get_premium(user_id: int) -> int:
    rows = query("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,))
    if not rows:
        return 0
    return rows[0]["expires_at"] if rows[0]["expires_at"] > now() else 0


def credit_referrer(buyer_id: int, charge_id: str, stars: int):
    """Зачисляет пригласившему REFERRAL_PERCENT процентов от оплаты."""
    rows = query("SELECT referred_by FROM users WHERE user_id=?", (buyer_id,))
    if not rows or not rows[0]["referred_by"]:
        return
    referrer = rows[0]["referred_by"]
    if referrer == buyer_id:
        return
    cut = max(0, int(round(stars * REFERRAL_PERCENT / 100)))
    if cut <= 0:
        return
    try:
        execute(
            """INSERT INTO referral_earnings(id, referrer_id, buyer_id, stars, percent, created_at, status)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (charge_id, referrer, buyer_id, cut, REFERRAL_PERCENT, now(), "earned"),
        )
    except Exception:
        pass


def referral_balance(user_id: int) -> int:
    rows = query(
        "SELECT COALESCE(SUM(stars),0) AS s FROM referral_earnings WHERE referrer_id=? AND status<>?",
        (user_id, "canceled"),
    )
    earned = rows[0]["s"] if rows else 0
    rows2 = query(
        "SELECT COALESCE(SUM(stars),0) AS s FROM payouts WHERE partner_id=? AND status IN (?,?)",
        (user_id, "requested", "paid"),
    )
    used = rows2[0]["s"] if rows2 else 0
    return int(earned) - int(used)


def user_public(user_id: int) -> dict:
    me = ensure_user(user_id) or {}
    exp = get_premium(user_id)
    invited = query("SELECT COUNT(*) AS c FROM users WHERE referred_by=?", (user_id,))[0]["c"]
    earned = query(
        "SELECT COALESCE(SUM(stars),0) AS s FROM referral_earnings WHERE referrer_id=? AND status<>?",
        (user_id, "canceled"),
    )[0]["s"]
    return {
        "premium": exp > 0,
        "expiresAt": exp,
        "refCode": me.get("ref_code"),
        "invited": int(invited),
        "earnedStars": int(earned),
        "balanceStars": referral_balance(user_id),
        "wallet": me.get("wallet"),
        "percent": REFERRAL_PERCENT,
    }


# ---------- проверка initData ----------
def validate_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        return None
    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return {}


# ---------- эндпоинты ----------
@app.get("/health")
def health():
    return {"ok": True, "bot": bool(BOT_TOKEN), "db": "postgres" if USE_PG else "sqlite", "time": now()}


@app.post("/stars/invoice")
async def stars_invoice(request: Request):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан")
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    plan_id = body.get("planId", "")
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(400, "Неизвестный тариф")
    user_id = user.get("id", 0)
    ensure_user(user_id, user.get("username"), user.get("first_name"))
    payload = f"{user_id}:{plan_id}:{now()}"
    data = {
        "title": plan["title"],
        "description": "Доступ ко всем практикам, энергодыханию и материалам",
        "payload": payload,
        "currency": "XTR",
        "prices": [{"label": plan["title"], "amount": plan["stars"]}],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{TG_API}/createInvoiceLink", json=data)
    j = r.json()
    if not j.get("ok"):
        raise HTTPException(502, f"Telegram: {j.get('description', 'error')}")
    return {"invoiceLink": j["result"], "payload": payload}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")
    update = await request.json()

    # Предоплата Stars — обязательный ответ
    if "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TG_API}/answerPreCheckoutQuery", json={"pre_checkout_query_id": pcq["id"], "ok": True})
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message") or {}
    frm = msg.get("from") or {}
    if frm.get("id"):
        ensure_user(frm["id"], frm.get("username"), frm.get("first_name"))

    # /start с реферальным кодом
    text = msg.get("text") or ""
    if text.startswith("/start"):
        parts = text.split()
        payload = parts[1] if len(parts) > 1 else ""
        if payload.startswith("ref_"):
            set_referral(frm.get("id", 0), payload[4:])

    sp = msg.get("successful_payment")
    if sp:
        payload = sp.get("invoice_payload", "")
        p = payload.split(":")
        user_id = int(p[0]) if p and p[0].isdigit() else frm.get("id", 0)
        plan_id = p[1] if len(p) > 1 else "month"
        plan = PLANS.get(plan_id, PLANS["month"])
        charge_id = sp.get("telegram_payment_charge_id", "") or f"{user_id}:{now()}"
        stars = int(sp.get("total_amount", plan["stars"]))
        ensure_user(user_id)
        # дедупликация по charge_id
        already = query("SELECT id FROM payments WHERE id=?", (charge_id,))
        if not already:
            execute(
                "INSERT INTO payments(id, user_id, plan_id, stars, created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (charge_id, user_id, plan_id, stars, now()),
            )
            expires = set_premium(user_id, plan_id, plan["days"])
            credit_referrer(user_id, charge_id, stars)
        else:
            expires = set_premium(user_id, plan_id, plan["days"])
        return {"ok": True, "user_id": user_id, "expires_at": expires}
    return {"ok": True}


@app.get("/premium")
def premium(initData: str = ""):
    user = validate_init_data(initData)
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    uid = user.get("id", 0)
    ensure_user(uid, user.get("username"), user.get("first_name"))
    exp = get_premium(uid)
    return {"premium": exp > 0, "expiresAt": exp}


@app.get("/me")
def me(initData: str = ""):
    user = validate_init_data(initData)
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    return user_public(user.get("id", 0))


@app.post("/referral/claim")
async def referral_claim(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    uid = user.get("id", 0)
    ensure_user(uid, user.get("username"), user.get("first_name"))
    set_referral(uid, body.get("code", ""))
    return user_public(uid)


@app.post("/referral/wallet")
async def referral_wallet(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    uid = user.get("id", 0)
    ensure_user(uid, user.get("username"), user.get("first_name"))
    wallet = (body.get("wallet") or "").strip()[:120]
    execute("UPDATE users SET wallet=? WHERE user_id=?", (wallet, uid))
    return user_public(uid)


@app.post("/referral/payout")
async def referral_payout(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    uid = user.get("id", 0)
    ensure_user(uid, user.get("username"), user.get("first_name"))
    me_row = query("SELECT wallet FROM users WHERE user_id=?", (uid,))
    wallet = (body.get("wallet") or (me_row[0]["wallet"] if me_row else "") or "").strip()[:120]
    if wallet:
        execute("UPDATE users SET wallet=? WHERE user_id=?", (wallet, uid))
    bal = referral_balance(uid)
    if bal <= 0:
        raise HTTPException(400, "Нет средств к выплате")
    if not wallet:
        raise HTTPException(400, "Укажите TON-кошелёк")
    execute(
        "INSERT INTO payouts(id, partner_id, stars, wallet, status, created_at) VALUES(?,?,?,?,?,?)",
        (secrets.token_urlsafe(10), uid, bal, wallet, "requested", now()),
    )
    return user_public(uid)


@app.get("/set_webhook")
def set_webhook(url: str, secret: str = ""):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан")
    data = {"url": url, "allowed_updates": ["message", "pre_checkout_query"]}
    if secret or WEBHOOK_SECRET:
        data["secret_token"] = secret or WEBHOOK_SECRET
    import urllib.request
    req = urllib.request.Request(
        f"{TG_API}/setWebhook",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
