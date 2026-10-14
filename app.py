"""
Бэкенд Telegram Mini App «Дыхание»: оплата Premium через Telegram Stars (XTR).

Что делает:
- проверяет подпись initData (HMAC-SHA256 по токену бота);
- создаёт счёт через Bot API createInvoiceLink (currency=XTR);
- принимает webhook successful_payment и активирует Premium в SQLite.

Переменные окружения:
- BOT_TOKEN       — токен бота от @BotFather (обязательно)
- WEBHOOK_SECRET  — секрет для заголовка вебхука (рекомендуется)
- DB_PATH         — путь к SQLite (по умолчанию premium.db)
- ALLOW_ORIGIN    — origin мини-аппа для CORS (по умолчанию *)
- PORT            — порт (по умолчанию 8080)
"""
import os
import hmac
import json
import time
import hashlib
import sqlite3
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
DB_PATH = os.environ.get("DB_PATH", "premium.db")
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")

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


# ---------- база ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscriptions (
                   user_id   INTEGER PRIMARY KEY,
                   plan_id   TEXT NOT NULL,
                   expires_at INTEGER NOT NULL,
                   updated_at INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER, plan_id TEXT, stars INTEGER,
                   charge_id TEXT, created_at INTEGER
               )"""
        )


init_db()


def set_premium(user_id: int, plan_id: str, days: int):
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
        base = max(now, row["expires_at"] if row else now)
        expires = base + days * 86400
        conn.execute(
            """INSERT INTO subscriptions(user_id, plan_id, expires_at, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 plan_id=excluded.plan_id, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
            (user_id, plan_id, expires, now),
        )
    return expires


def get_premium(user_id: int):
    with db() as conn:
        row = conn.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return 0
    return row["expires_at"] if row["expires_at"] > int(time.time()) else 0


# ---------- проверка initData ----------
def validate_init_data(init_data: str):
    """Возвращает dict пользователя или None. Алгоритм из документации Telegram."""
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
    return {"ok": True, "bot": bool(BOT_TOKEN), "time": int(time.time())}


@app.post("/stars/invoice")
async def stars_invoice(request: Request):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан")
    body = await request.json()
    init_data = body.get("initData", "")
    plan_id = body.get("planId", "")
    user = validate_init_data(init_data)
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    plan = PLANS.get(plan_id)
    if not plan:
        raise HTTPException(400, "Неизвестный тариф")

    user_id = user.get("id", 0)
    payload = f"{user_id}:{plan_id}:{int(time.time())}"
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

    msg = update.get("message") or update.get("edited_message") or {}
    sp = msg.get("successful_payment")
    if sp:
        payload = sp.get("invoice_payload", "")
        parts = payload.split(":")
        user_id = int(parts[0]) if parts and parts[0].isdigit() else msg.get("from", {}).get("id", 0)
        plan_id = parts[1] if len(parts) > 1 else "month"
        plan = PLANS.get(plan_id, PLANS["month"])
        expires = set_premium(user_id, plan_id, plan["days"])
        with db() as conn:
            conn.execute(
                "INSERT INTO payments(user_id, plan_id, stars, charge_id, created_at) VALUES(?,?,?,?,?)",
                (user_id, plan_id, sp.get("total_amount", plan["stars"]), sp.get("telegram_payment_charge_id", ""), int(time.time())),
            )
        return {"ok": True, "user_id": user_id, "expires_at": expires}
    return {"ok": True}


@app.get("/premium")
def premium(initData: str = ""):
    user = validate_init_data(initData)
    if user is None:
        raise HTTPException(401, "Неверная подпись initData")
    exp = get_premium(user.get("id", 0))
    return {"premium": exp > 0, "expiresAt": exp}


@app.get("/set_webhook")
def set_webhook(url: str, secret: str = ""):
    """Удобно для первичной настройки: /set_webhook?url=https://host/telegram/webhook&secret=..."""
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не задан")
    data = {"url": url, "allowed_updates": ["message"]}
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
