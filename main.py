import os
import secrets
from datetime import datetime, timezone, timedelta

import requests
import stripe
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client

app = FastAPI()

# -------------------------
# ENV
# -------------------------
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

TG_BOT_TOKEN = (os.getenv("TG_BOT_TOKEN", "") or "").strip()
TG_BOT_USERNAME = (os.getenv("TG_BOT_USERNAME", "") or "").lstrip("@").strip()

APP_URL = (os.getenv("APP_URL", "") or "").rstrip("/")
TG_GROUP_INVITE_LINK = (os.getenv("TG_GROUP_INVITE_LINK", "") or "").strip()
PLATFORM_URL = (os.getenv("PLATFORM_URL", "") or "").rstrip("/")
STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY", "") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET", "") or "").strip()
STRIPE_PRICE_ID = (os.getenv("STRIPE_PRICE_ID", "") or "").strip()
STRIPE_SUCCESS_URL = (os.getenv("STRIPE_SUCCESS_URL", "") or "").strip()
STRIPE_CANCEL_URL = (os.getenv("STRIPE_CANCEL_URL", "") or "").strip()

API_BASE_URL = (os.getenv("API_BASE_URL", "") or "").rstrip("/")  # опционально

TOKENS_TABLE = "link_tokens"

# Stripe key
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Supabase client
sb = create_client(SUPABASE_URL, SUPABASE_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None


# -------------------------
# Helpers
# -------------------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(*args):
    print("[pp-backend]", *args, flush=True)


def tg_send(chat_id: str, text: str, buttons=None, disable_preview: bool = False):
    """Отправка сообщения в Telegram."""
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


def tg_answer_callback(callback_query_id: str):
    """Убирает 'loading...' на кнопке."""
    if not TG_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
            timeout=8,
        )
    except Exception:
        pass


def safe_supabase_check():
    if not sb:
        raise RuntimeError("Supabase is not configured (missing SUPABASE_URL / SERVICE_ROLE_KEY)")
    return True


def issue_link(chat_id: str, source="tg", campaign="") -> str:
    """Создаём token в Supabase и возвращаем ссылку на Streamlit."""
    if not APP_URL:
        raise RuntimeError("Missing APP_URL")
    safe_supabase_check()

    token = secrets.token_urlsafe(16)
    row = {
        "token": token,
        "tg_chat_id": str(chat_id),
        "status": "issued",
        "created_at": utcnow_iso(),
        "source": source,
        "campaign": campaign or "EMPTY",
        "payment_status": "unpaid",
    }

    sb.table(TOKENS_TABLE).insert(row).execute()

    return f"{APP_URL}/?t={token}"


def upsert_chat_for_token(token: str, chat_id: str):
    safe_supabase_check()
    try:
        sb.table(TOKENS_TABLE).update({"tg_chat_id": str(chat_id)}).eq("token", token).execute()
    except Exception as e:
        log("upsert_chat_for_token failed:", repr(e))


def get_latest_token_for_chat(chat_id: str) -> str | None:
    safe_supabase_check()
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


def _fallback_url() -> str:
    if API_BASE_URL:
        return f"{API_BASE_URL}/health"
    return "https://example.com"


def create_checkout_for_token(token: str, chat_id: str) -> str:
    """Создаёт Stripe Checkout Session и пишет stripe_session_id в Supabase."""
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        raise RuntimeError("Stripe is not configured (missing STRIPE_SECRET_KEY / STRIPE_PRICE_ID)")
    safe_supabase_check()

    r = sb.table(TOKENS_TABLE).select("payment_status").eq("token", token).limit(1).execute()
    row = (r.data or [None])[0]
    if not row:
        raise RuntimeError("token_not_found")
    if (row.get("payment_status") or "").lower() == "paid":
        return ""

    success_url = STRIPE_SUCCESS_URL or _fallback_url()
    cancel_url = STRIPE_CANCEL_URL or _fallback_url()

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"token": token, "tg_chat_id": str(chat_id)},
    )

    sb.table(TOKENS_TABLE).update(
        {"stripe_session_id": session.id, "payment_status": "pending"}
    ).eq("token", token).execute()

    return session.url


def schedule_reminder(token: str, remind_type: str = "preview_20"):
    """Записываем напоминание (потом /jobs/reminders реально отправит)."""
    safe_supabase_check()
    try:
        sb.table(TOKENS_TABLE).update(
            {"remind_after": utcnow_iso(), "remind_type": remind_type}
        ).eq("token", token).execute()
    except Exception as e:
        log("schedule_reminder failed:", repr(e))


# -------------------------
# Health / Debug
# -------------------------
@app.get("/health")
def health():
    # 1) Будим Render
    resp = {"ok": True, "ts": utcnow_iso()}

    # 2) Трогаем Supabase (чтобы Supabase не поставил проект на паузу)
    if sb:
        try:
            sb.table(TOKENS_TABLE).select("token").limit(1).execute()
            resp["supabase"] = "ok"
        except Exception as e:
            log("health supabase ping failed:", repr(e))
            resp["supabase"] = "error"
    else:
        resp["supabase"] = "not_configured"

    return resp


@app.get("/debug/env")
def debug_env():
    mode = "unset"
    if STRIPE_SECRET_KEY.startswith("sk_test_"):
        mode = "test"
    elif STRIPE_SECRET_KEY.startswith("sk_live_"):
        mode = "live"
    return {
        "tg_bot_username": TG_BOT_USERNAME,
        "app_url_set": bool(APP_URL),
        "supabase_set": bool(SUPABASE_URL and SUPABASE_KEY),
        "stripe_key_mode": mode,
        "stripe_price_set": bool(STRIPE_PRICE_ID),
        "stripe_webhook_secret_set": bool(STRIPE_WEBHOOK_SECRET),
        "success_url": STRIPE_SUCCESS_URL,
        "cancel_url": STRIPE_CANCEL_URL,
    }


# -------------------------
# Reminders job (cron calls this)
# -------------------------
@app.post("/jobs/reminders")
def job_reminders():
    """
    Вызывается кроном раз в 5 минут.
    Реально шлёт напоминания через 20 минут после remind_after.
    """
    try:
        safe_supabase_check()
        now = datetime.now(timezone.utc)

        r = (
            sb.table(TOKENS_TABLE)
            .select("token,tg_chat_id,remind_after,remind_type,payment_status")
            .not_.is_("remind_type", "null")
            .execute()
        )

        rows = r.data or []
        sent = 0

        for row in rows:
            token = row.get("token")
            chat_id = row.get("tg_chat_id")
            remind_type = row.get("remind_type")
            payment_status = (row.get("payment_status") or "").lower()

            if not token:
                continue

            # если оплачено — сбрасываем напоминание
            if payment_status == "paid":
                try:
                    sb.table(TOKENS_TABLE).update({"remind_type": None, "remind_after": None}).eq("token", token).execute()
                except Exception:
                    pass
                continue

            if remind_type != "preview_20":
                continue

            ra = row.get("remind_after")
            if not ra:
                continue

            try:
                ra_dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
            except Exception:
                continue

            if now < (ra_dt + timedelta(minutes=20)):
                continue

            if not chat_id:
                # некуда отправить — сбросим
                try:
                    sb.table(TOKENS_TABLE).update({"remind_type": None, "remind_after": None}).eq("token", token).execute()
                except Exception:
                    pass
                continue

            # отправляем напоминание
            try:
                tg_send(
                    str(chat_id),
                    "Напоминаю про превью 👀\n\n"
                    "Хочешь открыть полный доступ?\n"
                    "Там 3 фокуса + план действий на каждый день.",
                    buttons=[
                        [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                sent += 1
            except Exception as e:
                log("job_reminders tg_send failed:", repr(e))

            # сбросим напоминание, чтобы не спамить
            try:
                sb.table(TOKENS_TABLE).update({"remind_type": None, "remind_after": None}).eq("token", token).execute()
            except Exception as e:
                log("job_reminders reset failed:", repr(e))

        return {"ok": True, "sent": sent}

    except Exception as e:
        log("job_reminders failed:", repr(e))
        return {"ok": False}


# -------------------------
# Stripe Webhook
# -------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    try:
        if not STRIPE_WEBHOOK_SECRET:
            log("missing STRIPE_WEBHOOK_SECRET")
            return JSONResponse({"ok": True})

        payload = await request.body()
        sig_header = request.headers.get("stripe-signature", "")

        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET,
            )
        except Exception as e:
            log("invalid_webhook:", repr(e))
            return JSONResponse({"error": "invalid_webhook"}, status_code=400)

        if event.get("type") == "checkout.session.completed":
            session = event["data"]["object"]
            token = (session.get("metadata") or {}).get("token")
            tg_chat_id = (session.get("metadata") or {}).get("tg_chat_id")

            log("checkout.session.completed token=", token, "chat=", tg_chat_id)

            if token and sb:
                try:
                    sb.table(TOKENS_TABLE).update(
                        {
                            "payment_status": "paid",
                            "paid_at": utcnow_iso(),
                            "stripe_session_id": session.get("id"),
                            "stripe_customer_email": (session.get("customer_details") or {}).get("email"),
                            "status": "paid",
                            "remind_type": None,
                            "remind_after": None,
                        }
                    ).eq("token", token).execute()
                except Exception as e:
                    log("supabase update after paid failed:", repr(e))

            if tg_chat_id:
                try:
                    platform_base = PLATFORM_URL or APP_URL or "https://example.com"
                    platform_link = f"{platform_base}/?t={token}&paid=1" if token else platform_base

                    buttons = [[{"text": "💠 Открыть платформу", "url": platform_link}]]

                    if TG_GROUP_INVITE_LINK:
                        buttons.append([{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}])

                    tg_send(
                        str(tg_chat_id),
                        "✅ Оплата прошла!\n\n"
                        "1) Открой платформу\n"
                        "2) Выбери фокус\n"
                        "3) Получи действия на сегодня 👇",
                        buttons=buttons,
                    )
                except Exception as e:
                    log("tg_send after paid failed:", repr(e))

        return JSONResponse({"ok": True})

    except Exception as e:
        log("stripe_webhook fatal:", repr(e))
        return JSONResponse({"ok": True})


# -------------------------
# Telegram Webhook
# -------------------------
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        return {"ok": True}

    try:
        cb = data.get("callback_query")
        if cb:
            cb_id = cb.get("id", "")
            if cb_id:
                tg_answer_callback(cb_id)

            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            action = (cb.get("data") or "").strip()

            if not chat_id:
                return {"ok": True}

            # START DIAG
            if action == "start_diag":
                try:
                    link = issue_link(chat_id, source="tg")
                    tg_send(
                        chat_id,
                        "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                        buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                    )
                except Exception as e:
                    log("issue_link failed:", repr(e))
                    tg_send(chat_id, "⚠️ Не могу выдать ссылку на диагностику (ошибка на сервере). Я уже чиню.")
                return {"ok": True}

            # PDF OK
            if action.startswith("pdf_ok:"):
                token = action.split("pdf_ok:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "Супер ✅\n\nХочешь посмотреть <b>превью</b> расширенного отчёта?",
                    buttons=[
                        [{"text": "👀 Показать превью", "callback_data": f"preview:{token}"}],
                        [{"text": "⏳ Позже", "callback_data": f"later:{token}"}],
                    ],
                )
                return {"ok": True}

            # PREVIEW
            if action.startswith("preview:") or action.startswith("offer:"):
                token = action.split(":", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                tg_send(
                    chat_id,
                    "👀 <b>Превью расширенной версии</b>\n\n"
                    "Вот что ты получишь в полной версии:\n"
                    "✅ 1) Расширенный отчёт — глубже и точнее\n"
                    "✅ 2) 3 фокуса реализации на ближайшие недели\n"
                    "✅ 3) План действий: что делать каждый день\n\n"
                    "Если хочешь — открою доступ сразу после оплаты 👇",
                    buttons=[
                        [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                        [{"text": "⏳ Напомнить позже", "callback_data": f"remind20:{token}"}],
                    ],
                )
                return {"ok": True}

            # REMIND 20
            if action.startswith("remind20:"):
                token = action.split("remind20:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)
                try:
                    schedule_reminder(token, "preview_20")
                except Exception as e:
                    log("remind20 schedule failed:", repr(e))
                tg_send(chat_id, "Ок 🙂 Напомню тебе через 20 минут.")
                return {"ok": True}

            # PAY
            if action.startswith("pay:"):
                token = action.split("pay:", 1)[1].strip()
                upsert_chat_for_token(token, chat_id)

                try:
                    checkout_url = create_checkout_for_token(token, chat_id)

                    if not checkout_url:
                        platform_base = PLATFORM_URL or APP_URL or "https://example.com"
                        platform_link = f"{platform_base}/?t={token}&paid=1" if token else platform_base

                        tg_send(
                            chat_id,
                            "✅ Похоже, доступ уже оплачен.\n\nНажми кнопку ниже 👇",
                            buttons=[[{"text": "💠 Открыть платформу", "url": platform_link}]],
                        )
                        return {"ok": True}

                    tg_send(
                        chat_id,
                        "💎 <b>Открываем полный доступ</b>\n\n"
                        "После оплаты ты получишь:\n"
                        "• полный расширенный отчёт\n"
                        "• 3 фокуса реализации\n"
                        "• план действий на каждый день\n\n"
                        "Оплата занимает ~30 секунд 👇",
                        buttons=[[{"text": "💳 Оплатить доступ", "url": checkout_url}]],
                    )
                    return {"ok": True}

                except Exception as e:
                    log("PAY_ERROR:", repr(e))
                    tg_send(chat_id, "Оплата пока не настроена 😕 (ошибка на сервере).")
                    return {"ok": True}

            # LATER
            if action.startswith("later:") or action == "remind_later":
                tg_send(chat_id, "Ок 🙂\n\nКогда будешь готов — напиши <b>превью</b>.")
                return {"ok": True}

            return {"ok": True}

        # normal message
        msg = data.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = (msg.get("text") or "").strip()

        if not chat_id:
            return {"ok": True}

        # /start
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            if start_payload.lower() in ("paid", "cancel"):
                return {"ok": True}

            if start_payload:
                token = start_payload
                upsert_chat_for_token(token, chat_id)

                tg_send(
                    chat_id,
                    "✅ Я вижу, что ты вернулась из диагностики.\n\n"
                    "Ты скачала PDF?\n"
                    "Если да — покажу следующий шаг 👇",
                    buttons=[
                        [{"text": "Я скачала PDF", "callback_data": f"pdf_ok:{token}"}],
                    ],
                )
                return {"ok": True}

            tg_send(
                chat_id,
                "Привет! Нажми кнопку — я выдам персональную ссылку на диагностику 👇",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
            return {"ok": True}

        # "превью"
        if text.lower() in ("превью", "preview", "показать превью"):
            try:
                token = get_latest_token_for_chat(chat_id)
            except Exception as e:
                log("get_latest_token_for_chat failed:", repr(e))
                token = None

            if not token:
                tg_send(chat_id, "Я не вижу твою последнюю диагностику 😕 Нажми «✨ Начать».")
                return {"ok": True}

            tg_send(
                chat_id,
                "👀 <b>Превью расширенной версии</b>\n\n"
                "Вот что ты получишь в полной версии:\n"
                "✅ 1) Расширенный отчёт — глубже и точнее\n"
                "✅ 2) 3 фокуса реализации на ближайшие недели\n"
                "✅ 3) План действий: что делать каждый день\n\n"
                "Если хочешь — открою доступ сразу после оплаты 👇",
                buttons=[
                    [{"text": "💎 Открыть полный доступ", "callback_data": f"pay:{token}"}],
                    [{"text": "⏳ Напомнить позже", "callback_data": f"remind20:{token}"}],
                ],
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
        log("tg_webhook fatal:", repr(e))
        return {"ok": True}