import os
import secrets
from datetime import datetime, timezone

import requests
import stripe
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client

app = FastAPI()

# -------------------------
# ENV
# -------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")  # service role key
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

APP_URL = os.getenv("APP_URL", "").rstrip("/")  # Streamlit URL, без / в конце
TG_GROUP_INVITE_LINK = os.getenv("TG_GROUP_INVITE_LINK", "")
PAY_URL = os.getenv("PAY_URL", "")  # optional (не обязательно)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

TOKENS_TABLE = "link_tokens"


# -------------------------
# Helpers
# -------------------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tg_send(chat_id: str, text: str, buttons=None):
    if not TG_BOT_TOKEN:
        raise RuntimeError("Missing TG_BOT_TOKEN")

    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}

    r = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=12,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text}")


def issue_link(chat_id: str, source="tg", campaign=""):
    if not APP_URL:
        raise RuntimeError("Missing APP_URL")

    token = secrets.token_urlsafe(16)

    sb.table(TOKENS_TABLE).insert(
        {
            "token": token,
            "tg_chat_id": str(chat_id),
            "source": source,
            "campaign": campaign,
            "status": "issued",
            "created_at": utcnow_iso(),
        }
    ).execute()

    return f"{APP_URL}/?t={token}"


def get_token_row(token: str):
    r = sb.table(TOKENS_TABLE).select("*").eq("token", token).limit(1).execute()
    rows = r.data or []
    return rows[0] if rows else None


def ensure_token_chat_link(token: str, chat_id: str):
    # привязываем токен к чату (на случай если tg_chat_id пустой)
    try:
        sb.table(TOKENS_TABLE).update({"tg_chat_id": str(chat_id)}).eq("token", token).execute()
    except Exception:
        pass


def create_stripe_checkout(token: str, chat_id: str):
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise RuntimeError("stripe_not_configured")

    row = get_token_row(token)
    if not row:
        raise RuntimeError("token_not_found")

    # если уже оплачено — можно не создавать повторно
    if row.get("payment_status") == "paid":
        return None, None

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=STRIPE_SUCCESS_URL or "https://t.me/",
        cancel_url=STRIPE_CANCEL_URL or "https://t.me/",
        metadata={"token": token, "tg_chat_id": str(chat_id)},
    )

    sb.table(TOKENS_TABLE).update(
        {"stripe_session_id": session.id, "payment_status": "pending"}
    ).eq("token", token).execute()

    return session.url, session.id


# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


# -------------------------
# Token status API (для проверки)
# -------------------------
@app.get("/api/token/{token}")
def api_get_token(token: str):
    try:
        r = (
            sb.table(TOKENS_TABLE)
            .select("token,status,created_at,completed_at,session_id,tg_chat_id,source,campaign,payment_status,paid_at")
            .eq("token", token)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="token_not_found")
        return rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"server_error: {e}")


# -------------------------
# Stripe: create checkout (optional endpoint)
# -------------------------
@app.post("/pay/create-checkout")
def pay_create_checkout(token: str):
    try:
        row = get_token_row(token)
        if not row:
            raise HTTPException(status_code=404, detail="token_not_found")

        if row.get("payment_status") == "paid":
            return {"ok": True, "already_paid": True, "checkout_url": None}

        checkout_url, session_id = create_stripe_checkout(token, row.get("tg_chat_id") or "")
        return {"ok": True, "checkout_url": checkout_url, "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_checkout_error: {e}")


# -------------------------
# Stripe webhook
# -------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="missing_STRIPE_WEBHOOK_SECRET")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid_webhook: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata") or {}
        token = (meta.get("token") or "").strip()
        tg_chat_id = (meta.get("tg_chat_id") or "").strip()

        if token:
            sb.table(TOKENS_TABLE).update(
                {
                    "payment_status": "paid",
                    "paid_at": utcnow_iso(),
                    "stripe_session_id": session.get("id"),
                    "stripe_customer_email": (session.get("customer_details") or {}).get("email"),
                    "status": "paid",
                }
            ).eq("token", token).execute()

        if tg_chat_id:
            platform_link = APP_URL
            buttons = [[{"text": "💠 Открыть платформу", "url": platform_link}]]
            if TG_GROUP_INVITE_LINK:
                buttons.append([{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}])

            tg_send(
                tg_chat_id,
                "✅ Оплата прошла!\n\nЯ открыла доступ — нажми кнопку ниже:",
                buttons=buttons,
            )

    return JSONResponse({"ok": True})


# -------------------------
# Telegram webhook
# -------------------------
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    try:
        data = await req.json()

        # 1) callback (нажатие кнопок)
        cb = data.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            action = (cb.get("data") or "").strip()

            if not chat_id:
                return {"ok": True}

            # start diag
            if action == "start_diag":
                link = issue_link(chat_id, source="tg")
                tg_send(
                    chat_id,
                    "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                    buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                )
                return {"ok": True}

            # user says: "I finished"
            if action.startswith("done:"):
                token = action.split("done:", 1)[1].strip()
                if token:
                    ensure_token_chat_link(token, chat_id)

                tg_send(
                    chat_id,
                    "🔥 Супер, ты почти на финише.\n\n"
                    "Хочешь, покажу «что внутри» расширенной версии? Без оплаты — просто превью.\n\n"
                    "Там:\n"
                    "• расширенный отчёт (глубже)\n"
                    "• 3 фокуса\n"
                    "• реализация: что делать каждый день",
                    buttons=[
                        [{"text": "👀 Посмотреть превью", "callback_data": f"offer:{token}"}],
                        [{"text": "📌 Напомнить позже", "callback_data": "remind_later"}],
                    ],
                )
                return {"ok": True}

            # preview offer
            if action.startswith("offer:"):
                token = action.split("offer:", 1)[1].strip()
                if token:
                    ensure_token_chat_link(token, chat_id)

                tg_send(
                    chat_id,
                    "👀 Превью расширения:\n\n"
                    "✅ Сильные стороны — без воды, с примерами\n"
                    "✅ Где ты теряешь энергию и почему\n"
                    "✅ 3 фокуса на ближайшие недели\n"
                    "✅ Мини-план действий (очень понятный)\n\n"
                    "Если хочешь — открою полный доступ.",
                    buttons=[
                        [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                        [{"text": "↩️ Я ещё прохожу", "callback_data": f"resume:{token}"}],
                    ],
                )
                return {"ok": True}

            # resume diagnostic
            if action.startswith("resume:"):
                token = action.split("resume:", 1)[1].strip()
                if token:
                    ensure_token_chat_link(token, chat_id)

                tg_send(
                    chat_id,
                    "Ок 🙂 Продолжай диагностику тут:",
                    buttons=[[{"text": "▶️ Продолжить диагностику", "url": f"{APP_URL}/?t={token}"}]],
                )
                return {"ok": True}

            # pay -> create checkout now
            if action.startswith("pay:"):
                token = action.split("pay:", 1)[1].strip()
                if token:
                    ensure_token_chat_link(token, chat_id)

                try:
                    checkout_url, _sid = create_stripe_checkout(token, chat_id)

                    if not checkout_url:
                        tg_send(
                            chat_id,
                            "Похоже, доступ уже активирован ✅\n\nНажми «Открыть платформу».",
                            buttons=[[{"text": "💠 Открыть платформу", "url": APP_URL}]],
                        )
                        return {"ok": True}

                    tg_send(
                        chat_id,
                        "Готово ✅ Я подготовила оплату.\n\n"
                        "После оплаты я сразу открою доступ и отправлю материалы.",
                        buttons=[
                            [{"text": "💳 Перейти к оплате", "url": checkout_url}],
                            *([[{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}]] if TG_GROUP_INVITE_LINK else []),
                        ],
                    )
                except Exception as e:
                    tg_send(chat_id, f"Не смогла создать оплату 😕 ({e})")

                return {"ok": True}

            # remind later
            if action == "remind_later":
                tg_send(chat_id, "Ок 🙂 Когда будешь готов — напиши в чат слово: превью")
                return {"ok": True}

            return {"ok": True}

        # 2) обычный текст (сообщения)
        msg = data.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = (msg.get("text") or "").strip()

        if not chat_id:
            return {"ok": True}

        # /start with optional token payload
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            if start_payload:
                token = start_payload
                ensure_token_chat_link(token, chat_id)

                tg_send(
                    chat_id,
                    "Ты вернулся из диагностики ✅\n\n"
                    "Если ты ещё проходишь — нажми «Продолжить».\n"
                    "Если уже закончил — нажми «Я прошёл ✅».",
                    buttons=[
                        [{"text": "▶️ Продолжить диагностику", "url": f"{APP_URL}/?t={token}"}],
                        [{"text": "✅ Я прошёл", "callback_data": f"done:{token}"}],
                    ],
                )
                return {"ok": True}

            # обычный старт без токена
            tg_send(
                chat_id,
                "Привет! Я бот диагностики Personal Potentials.\n\nНажми кнопку — я выдам персональную ссылку.",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
            return {"ok": True}

        # слово "превью" (на случай remind later)
        if text.lower() in ("превью", "preview"):
            tg_send(
                chat_id,
                "Ок 🙂 Скажи, пожалуйста: ты уже прошёл диагностику?\n\n"
                "Если да — отправь мне команду /start <твой_токен>\n"
                "Её можно получить из ссылки диагностики (параметр t=...)."
            )
            return {"ok": True}

        # fallback
        tg_send(
            chat_id,
            "Нажми «✨ Начать», и я выдам персональную ссылку.",
            buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
        )
        return {"ok": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"webhook_error: {e}")