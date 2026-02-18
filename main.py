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
TG_BOT_USERNAME = (os.getenv("TG_BOT_USERNAME", "") or "").lstrip("@").strip()

APP_URL = (os.getenv("APP_URL", "") or "").rstrip("/")
TG_GROUP_INVITE_LINK = os.getenv("TG_GROUP_INVITE_LINK", "")

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

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
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
            "payment_status": None,
        }
    ).execute()

    return f"{APP_URL}/?t={token}"


def upsert_chat_for_token(token: str, chat_id: str):
    try:
        sb.table(TOKENS_TABLE).update({"tg_chat_id": str(chat_id)}).eq("token", token).execute()
    except Exception:
        pass


def build_tg_return_link(token: str) -> str:
    # в start payload нельзя пробелы, лучше не усложнять
    # Telegram допускает payload до 64 символов — наш token_urlsafe обычно ок
    if not TG_BOT_USERNAME:
        return "https://t.me/"
    return f"https://t.me/{TG_BOT_USERNAME}?start={token}"


def create_checkout_for_token(token: str, chat_id: str) -> str:
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="stripe_not_configured")

    r = sb.table(TOKENS_TABLE).select("*").eq("token", token).limit(1).execute()
    row = (r.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="token_not_found")

    if row.get("payment_status") == "paid":
        return ""

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

    return session.url


# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


# -------------------------
# Token status API
# -------------------------
@app.get("/api/token/{token}")
def get_token(token: str):
    try:
        r = (
            sb.table(TOKENS_TABLE)
            .select("token,status,created_at,completed_at,session_id,tg_chat_id,source,campaign,payment_status")
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
# Stripe Webhook
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

        if tg_chat_id:
            platform_link = APP_URL
            buttons = [[{"text": "💠 Открыть платформу", "url": platform_link}]]
            if TG_GROUP_INVITE_LINK:
                buttons.append([{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}])

            tg_send(
                str(tg_chat_id),
                "✅ Оплата прошла!\n\nЯ открыла доступ к расширенным материалам. Нажми кнопку ниже 👇",
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
        msg = data.get("message") or (data.get("callback_query") or {}).get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")

        if not chat_id:
            return {"ok": True}

        # ---------- CALLBACKS ----------
        cb = data.get("callback_query")
        if cb:
            action = (cb.get("data") or "").strip()

            # старт диагностики
            if action == "start_diag":
                link = issue_link(chat_id, source="tg")
                tg_send(
                    chat_id,
                    "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                    buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                )
                return {"ok": True}

            # пользователь: "да, PDF скачал"
            if action.startswith("pdf_ok:"):
                token = action.split("pdf_ok:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "Супер ✅\n\n"
                    "Тогда самый интересный вопрос: хочешь увидеть <b>превью</b> расширенного отчёта?\n\n"
                    "Я покажу, какие блоки там есть и чем он отличается от бесплатного.",
                    buttons=[
                        [{"text": "👀 Показать превью", "callback_data": f"preview:{token}"}],
                        [{"text": "🕒 Напомнить позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # превью расширения (мягко, без цены в лоб)
            if action.startswith("preview:"):
                token = action.split("preview:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "👀 <b>Превью расширенной версии</b>\n\n"
                    "Внутри будет:\n"
                    "• более точная расшифровка твоих сильных сторон (на языке поведения)\n"
                    "• где ты теряешь энергию и почему\n"
                    "• 3 фокуса на ближайшие недели\n"
                    "• «реализация» — понятный план действий\n\n"
                    "Если хочешь — я открою доступ одним кликом.",
                    buttons=[
                        [{"text": "💎 Хочу открыть доступ", "callback_data": f"unlock:{token}"}],
                        [{"text": "↩️ Вернуться", "callback_data": f"welcome:{token}"}],
                    ],
                )
                return {"ok": True}

            # только тут создаём оплату
            if action.startswith("unlock:"):
                token = action.split("unlock:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                try:
                    checkout_url = create_checkout_for_token(token, chat_id)
                except Exception:
                    tg_send(chat_id, "Оплата пока не настроена 😕 Попробуй чуть позже.")
                    return {"ok": True}

                if not checkout_url:
                    tg_send(chat_id, "Похоже, доступ уже оплачен ✅")
                    return {"ok": True}

                tg_send(
                    chat_id,
                    "Готово ✅\n\n"
                    "Я подготовила оплату. После оплаты я автоматически пришлю доступ.",
                    buttons=[
                        [{"text": "💳 Перейти к оплате", "url": checkout_url}],
                        [{"text": "🕒 Подумать позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # напомнить позже
            if action.startswith("later:"):
                token = action.split("later:", 1)[1].strip()
                tg_send(
                    chat_id,
                    "Ок 🙂\n\n"
                    "Когда будешь готов — просто напиши <b>превью</b> (и я покажу ещё раз).",
                )
                return {"ok": True}

            # повторить “welcome”
            if action.startswith("welcome:"):
                token = action.split("welcome:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "✅ Ты вернулся из диагностики.\n\n"
                    "Небольшой момент: ты успел(а) скачать PDF?\n\n"
                    "Если нет — открою диагностику снова на твоей ссылке.",
                    buttons=[
                        [{"text": "📄 Я скачал(а) PDF", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть PDF ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            return {"ok": True}

        # ---------- TEXT MESSAGES ----------
        text = (data.get("message", {}) or {}).get("text", "") or ""
        text = text.strip()

        # /start (с payload или без)
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""

            if payload:
                token = payload
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "✨ <b>Ты на финише!</b>\n\n"
                    "Судя по всему, ты вернулся(лась) после диагностики.\n\n"
                    "Скажи честно: PDF уже скачал(а)?",
                    buttons=[
                        [{"text": "📄 Да, скачал(а)", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть PDF ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            # обычный старт без payload
            tg_send(
                chat_id,
                "Привет! Я бот диагностики Personal Potentials.\n\nНажми кнопку — я выдам персональную ссылку 👇",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
            return {"ok": True}

        # если человек написал "превью"
        if text.lower() in ("превью", "preview"):
            tg_send(
                chat_id,
                "Ок 🙂\n\nЕсли ты заходил(а) из диагностики, нажми кнопку «Вернуться в Telegram» там ещё раз.\n"
                "Или отправь мне токен (набор букв/цифр из ссылки) — я продолжу.",
            )
            return {"ok": True}

        # дефолт
        tg_send(
            chat_id,
            "Нажми «✨ Начать», и я выдам персональную ссылку на диагностику.",
            buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
        )
        return {"ok": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"webhook_error: {e}")


# -------------------------
# (Не используем) complete endpoint — можно оставить на будущее
# -------------------------
class CompleteIn(BaseModel):
    token: str
    session_id: str
    client_name: str | None = "Клиент"


@app.post("/complete")
def complete(inp: CompleteIn):
    # оставили заглушку, чтобы ничего не ломалось, если где-то ещё дергается
    return {"ok": True, "note": "complete_not_used_in_current_flow"}