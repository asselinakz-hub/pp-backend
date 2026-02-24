import os
import secrets
from datetime import datetime, timezone
from typing import Optional

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
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_BOT_USERNAME = (os.getenv("TG_BOT_USERNAME", "") or "").lstrip("@").strip()

APP_URL = (os.getenv("APP_URL", "") or "").rstrip("/")
TG_GROUP_INVITE_LINK = (os.getenv("TG_GROUP_INVITE_LINK", "") or "").strip()

STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY", "") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET", "") or "").strip()
STRIPE_PRICE_ID = (os.getenv("STRIPE_PRICE_ID", "") or "").strip()
STRIPE_SUCCESS_URL = (os.getenv("STRIPE_SUCCESS_URL", "") or "").strip()
STRIPE_CANCEL_URL = (os.getenv("STRIPE_CANCEL_URL", "") or "").strip()

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


def tg_send(chat_id: str, text: str, buttons=None, disable_preview: bool = False):
    if not TG_BOT_TOKEN:
        raise RuntimeError("Missing TG_BOT_TOKEN")

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}

    r = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=15,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    if not TG_BOT_TOKEN:
        return False

    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    r = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
        json=payload,
        timeout=10,
    )
    return r.status_code < 400


def issue_link(chat_id: str, source="tg", campaign="") -> str:
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
            "payment_status": None,
        }
    ).execute()

    return f"{APP_URL}/?t={token}"


def upsert_chat_for_token(token: str, chat_id: str):
    try:
        sb.table(TOKENS_TABLE).update({"tg_chat_id": str(chat_id)}).eq("token", token).execute()
    except Exception:
        pass


def get_latest_token_for_chat(chat_id: str) -> Optional[str]:
    r = (
        sb.table(TOKENS_TABLE)
        .select("token,created_at")
        .eq("tg_chat_id", str(chat_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    row = (r.data or [None])[0]
    return (row or {}).get("token")


def create_checkout_for_token(token: str, chat_id: str) -> str:
    # Жёстко валидируем конфиг, чтобы не было "оплата не настроена"
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise RuntimeError("stripe_not_configured: missing STRIPE_SECRET_KEY or STRIPE_PRICE_ID")

    # Проверим токен в базе
    r = sb.table(TOKENS_TABLE).select("*").eq("token", token).limit(1).execute()
    row = (r.data or [None])[0]
    if not row:
        raise RuntimeError("token_not_found")

    if row.get("payment_status") == "paid":
        return ""  # уже оплачено

    # Если success/cancel не заданы — вернём в бот по username
    # (важно: username, НЕ токен!)
    fallback_success = f"https://t.me/{TG_BOT_USERNAME}?start=paid" if TG_BOT_USERNAME else "https://t.me/"
    fallback_cancel = f"https://t.me/{TG_BOT_USERNAME}?start=cancel" if TG_BOT_USERNAME else "https://t.me/"

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=STRIPE_SUCCESS_URL or fallback_success,
        cancel_url=STRIPE_CANCEL_URL or fallback_cancel,
        metadata={"token": token, "tg_chat_id": str(chat_id)},
    )

    sb.table(TOKENS_TABLE).update(
        {"stripe_session_id": session.id, "payment_status": "pending"}
    ).eq("token", token).execute()

    return session.url


# -------------------------
# Health / Debug
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug/env")
def debug_env():
    # ВАЖНО: без секретов, только чтобы понять test/live
    return {
        "tg_bot_username": TG_BOT_USERNAME,
        "app_url_set": bool(APP_URL),
        "stripe_key_mode": ("test" if STRIPE_SECRET_KEY.startswith("sk_test_") else ("live" if STRIPE_SECRET_KEY.startswith("sk_live_") else "missing")),
        "stripe_price_set": bool(STRIPE_PRICE_ID),
        "stripe_webhook_secret_set": bool(STRIPE_WEBHOOK_SECRET),
        "success_url": STRIPE_SUCCESS_URL,
        "cancel_url": STRIPE_CANCEL_URL,
    }


# -------------------------
# Token status API
# -------------------------
@app.get("/api/token/{token}")
def get_token(token: str):
    r = (
        sb.table(TOKENS_TABLE)
        .select("token,status,created_at,completed_at,session_id,tg_chat_id,source,campaign,payment_status,paid_at,stripe_session_id")
        .eq("token", token)
        .limit(1)
        .execute()
    )
    rows = r.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="token_not_found")
    return rows[0]


# -------------------------
# Stripe Webhook
# -------------------------
@app.get("/stripe/webhook")
def stripe_webhook_get():
    # чтобы при открытии в браузере не пугало Method Not Allowed
    return {"ok": True, "note": "Stripe webhooks must be sent via POST"}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        # Stripe увидит 500 и будет ретраить. Но это лучше чем молча принять.
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
        # Stripe ретраит на 400? обычно нет, но пусть видит ошибку подписи
        raise HTTPException(status_code=400, detail=f"invalid_webhook: {e}")

    etype = event.get("type")

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        token = (session.get("metadata") or {}).get("token")
        tg_chat_id = (session.get("metadata") or {}).get("tg_chat_id")

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

        # Сообщаем в Telegram
        if tg_chat_id:
            platform_link = APP_URL or "https://t.me/"
            buttons = [[{"text": "💠 Открыть платформу", "url": platform_link}]]

            if TG_GROUP_INVITE_LINK:
                buttons.append([{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}])

            tg_send(
                str(tg_chat_id),
                "✅ Оплата прошла!\n\nЯ открыла доступ. Нажми кнопку ниже 👇",
                buttons=buttons,
            )

    return JSONResponse({"ok": True})


# -------------------------
# Telegram webhook
# -------------------------
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    """
    КРИТИЧНО: Telegram должен всегда получать 200 OK.
    Любые ошибки логируем, но возвращаем {"ok": True}.
    """
    try:
        data = await req.json()

        # 1) Callback query
        cb = data.get("callback_query")
        if cb:
            cb_id = cb.get("id", "")
            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            action = (cb.get("data") or "").strip()

            if cb_id:
                tg_answer_callback(cb_id)

            if not chat_id:
                return {"ok": True}

            # START DIAG
            if action == "start_diag":
                try:
                    link = issue_link(chat_id, source="tg")
                except Exception as e:
                    print("START_DIAG_ERROR:", repr(e))
                    tg_send(chat_id, "⚠️ Не могу выдать ссылку на диагностику (ошибка на сервере). Я уже чиню.")
                    return {"ok": True}

                try:
                    tg_send(
                        chat_id,
                        "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                        buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                    )
                except Exception as e:
                    print("TG_SEND_ERROR:", repr(e))
                return {"ok": True}

            # PDF OK
            if action.startswith("pdf_ok:"):
                token = action.split("pdf_ok:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "Супер ✅\n\nХочешь посмотреть <b>превью</b> расширенной версии?",
                    buttons=[
                        [{"text": "👀 Показать превью", "callback_data": f"preview:{token}"}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # PREVIEW
            if action.startswith("preview:"):
                token = action.split("preview:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "👀 <b>Превью расширенной версии</b>\n\n"
                    "✅ 1) Расширенный отчёт — глубже и точнее\n"
                    "✅ 2) 3 фокуса на ближайшие недели\n"
                    "✅ 3) Простая реализация: что делать каждый день\n\n"
                    "Открыть полный доступ?",
                    buttons=[
                        [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # PAY (Stripe checkout)
            if action.startswith("pay:"):
                token = action.split("pay:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                try:
                    checkout_url = create_checkout_for_token(token, chat_id)
                except Exception as e:
                    print("PAY ERROR:", repr(e))
                    tg_send(chat_id, "Оплата пока не настроена 😕 (проверь Stripe ENV в Render)")
                    return {"ok": True}

                if not checkout_url:
                    tg_send(chat_id, "Похоже, доступ уже оплачен ✅")
                    return {"ok": True}

                tg_send(
                    chat_id,
                    "Готово ✅\n\nПерейди к оплате по кнопке ниже.\nПосле оплаты я пришлю доступ автоматически.",
                    buttons=[[{"text": "💳 Перейти к оплате", "url": checkout_url}]],
                )
                return {"ok": True}

            # LATER
            if action.startswith("later:") or action == "later":
                tg_send(chat_id, "Ок 🙂 Когда будешь готов — напиши <b>превью</b>.")
                return {"ok": True}

            # WELCOME
            if action.startswith("welcome:"):
                token = action.split("welcome:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "✅ Я вижу, что ты вернулся(ась) из диагностики.\n\nСкачал(а) PDF-отчёт?",
                    buttons=[
                        [{"text": "📥 Я скачал(а) PDF", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть диагностику ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            return {"ok": True}

        # 2) Message
        msg = data.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = (msg.get("text") or "").strip()

        if not chat_id:
            return {"ok": True}

        # /start payload
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            if start_payload:
                token = start_payload
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "✅ Я вижу, что ты вернулся(ась) из диагностики.\n\nСкачал(а) PDF-отчёт?",
                    buttons=[
                        [{"text": "📥 Я скачал(а) PDF", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть диагностику ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            tg_send(
                chat_id,
                "Привет! Нажми кнопку — я выдам персональную ссылку на диагностику 👇",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
            return {"ok": True}

        # text "превью"
        if text.lower() in ("превью", "preview", "показать превью"):
            token = get_latest_token_for_chat(chat_id)
            if not token:
                tg_send(chat_id, "Я не вижу твою последнюю диагностику 😕 Нажми «✨ Начать» ещё раз.")
                return {"ok": True}

            tg_send(
                chat_id,
                "👀 <b>Превью расширенной версии</b>\n\n"
                "✅ 1) Расширенный отчёт\n"
                "✅ 2) 3 фокуса\n"
                "✅ 3) Ежедневная реализация\n\n"
                "Открыть полный доступ?",
                buttons=[[{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}]],
            )
            return {"ok": True}

        # default
        tg_send(
            chat_id,
            "Нажми «✨ Начать», и я выдам персональную ссылку на диагностику.",
            buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
        )
        return {"ok": True}

    except Exception as e:
        print("TG_WEBHOOK_ERROR:", repr(e))
        return {"ok": True}


# -------------------------
# Optional (future)
# -------------------------
class CompleteIn(BaseModel):
    token: str
    session_id: str
    client_name: str | None = "Клиент"


@app.post("/complete")
def complete(inp: CompleteIn):
    return {"ok": True, "note": "complete_not_used_in_current_flow"}