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
        timeout=12,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    """ВАЖНО: убирает 'loading...' на нажатой inline-кнопке."""
    if not TG_BOT_TOKEN:
        raise RuntimeError("Missing TG_BOT_TOKEN")
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    r = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
        json=payload,
        timeout=12,
    )
    # тут не валим сервер, даже если не получилось
    return r.status_code < 400


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


def get_latest_token_for_chat(chat_id: str) -> str | None:
    r = (
        sb.table(TOKENS_TABLE)
        .select("token,created_at")
        .eq("tg_chat_id", str(chat_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    row = (r.data or [None])[0]
    token = (row or {}).get("token")
    return token


def create_checkout_for_token(token: str, chat_id: str) -> str:
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="stripe_not_configured")

    r = sb.table(TOKENS_TABLE).select("*").eq("token", token).limit(1).execute()
    row = (r.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="token_not_found")

    if row.get("payment_status") == "paid":
        return ""  # уже оплачено

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

        # 1) Callback query (нажатие inline-кнопок)
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
                link = issue_link(chat_id, source="tg")
                tg_send(
                    chat_id,
                    "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                    buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                )
                return {"ok": True}

            # PDF OK
            if action.startswith("pdf_ok:"):
                token = action.split("pdf_ok:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "Супер ✅\n\n"
                    "Хочешь посмотреть <b>превью</b> расширенного отчёта?\n"
                    "Я покажу, какие блоки там есть и чем он отличается от бесплатного.",
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

                # ВАЖНО: тут лучше НЕ надеяться на кликабельность превью-картинки.
                # Даем понятную кнопку и в тексте можно дать ссылку.
                tg_send(
                    chat_id,
                    "👀 <b>Превью расширенной версии</b>\n\n"
                    "Внутри будет:\n"
                    "✅ 1) Расширенный отчёт — глубже, точнее, с расшифровкой твоих механизмов\n"
                    "✅ 2) 3 фокуса на ближайшие недели (без воды)\n"
                    "✅ 3) Простая реализация: что делать каждый день, чтобы реально сдвинуться\n\n"
                    "Если хочешь — я открою доступ одним кликом 👇",
                    buttons=[
                        [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # PAY (создаем оплату)
            if action.startswith("pay:"):
                token = action.split("pay:", 1)[1].strip()
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
                    "Перейди к оплате по кнопке ниже. После оплаты я автоматически пришлю доступ.",
                    buttons=[
                        [{"text": "💳 Перейти к оплате", "url": checkout_url}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # LATER / REMIND_LATER
            if action == "remind_later" or action.startswith("later:"):
                tg_send(
                    chat_id,
                    "Ок 🙂\n\n"
                    "Когда будешь готов — напиши <b>превью</b> или нажми кнопку «👀 Показать превью», и я покажу снова.",
                )
                return {"ok": True}

            # WELCOME (вернуться к вопросу про PDF)
            if action.startswith("welcome:"):
                token = action.split("welcome:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "✅ Ты вернулся(ась) из диагностики.\n\n"
                    "Скачал(а) PDF?\n\n"
                    "Если нет — открою диагностику снова на твоей ссылке.",
                    buttons=[
                        [{"text": "📄 Я скачал(а) PDF", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть PDF ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            return {"ok": True}

        # 2) Обычные сообщения (message.text)
        msg = data.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = (msg.get("text") or "").strip()

        if not chat_id:
            return {"ok": True}

        # /start с payload
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            # Если пришли из диагностики: payload == token
            if start_payload:
                token = start_payload
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "✅ Я вижу, что ты вернулся(ась) из диагностики.\n\n"
                    "Скачал(а) PDF-отчёт?\n"
                    "Если да — покажу следующий шаг 👇",
                    buttons=[
                        [{"text": "📥 Я скачал(а) PDF", "callback_data": f"pdf_ok:{token}"}],
                        [{"text": "↩️ Открыть диагностику ещё раз", "url": f"{APP_URL}/?t={token}"}],
                    ],
                )
                return {"ok": True}

            # обычный /start без токена
            tg_send(
                chat_id,
                "Привет! Нажми кнопку — я выдам персональную ссылку на диагностику 👇",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
            return {"ok": True}

        # Если человек написал "превью" текстом — покажем превью по последнему token
        if text.lower() in ("превью", "preview", "показать превью", "👀 показать превью"):
            token = get_latest_token_for_chat(chat_id)
            if not token:
                tg_send(chat_id, "Я не вижу твою последнюю диагностику 😕 Нажми «✨ Начать» ещё раз.")
                return {"ok": True}

            tg_send(
                chat_id,
                "👀 <b>Превью расширенной версии</b>\n\n"
                "Внутри будет:\n"
                "✅ 1) Расширенный отчёт — глубже, точнее, с расшифровкой твоих механизмов\n"
                "✅ 2) 3 фокуса на ближайшие недели (без воды)\n"
                "✅ 3) Простая реализация: что делать каждый день, чтобы реально сдвинуться\n\n"
                "Хочешь, я открою полный доступ?",
                buttons=[
                    [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                    [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                ],
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
    return {"ok": True, "note": "complete_not_used_in_current_flow"}