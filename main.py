import os
import secrets
from datetime import datetime, timezone
import stripe
from fastapi.responses import JSONResponse

import requests
from fastapi import FastAPI, Request, HTTPException
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
PAY_URL = os.getenv("PAY_URL", "")  # optional

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

if not SUPABASE_URL or not SUPABASE_KEY:
    # Render покажет это в логах при старте
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

TOKENS_TABLE = "link_tokens"


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
    # если токен бота неверный/бот не доступен — увидим это в логах
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


# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


# -------------------------
# Token status API (для проверки/бота)
# -------------------------
@app.get("/api/token/{token}")
def get_token(token: str):
    try:
        r = (
            sb.table(TOKENS_TABLE)
            .select("token,status,created_at,completed_at,session_id,tg_chat_id,source,campaign")
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

@app.post("/pay/create-checkout")
def create_checkout(token: str):
    """
    token = твой link_token из таблицы link_tokens
    Возвращаем url Stripe Checkout, куда бот/пользователь перейдёт
    """
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="stripe_not_configured")

    # 1) проверяем токен
    r = sb.table(TOKENS_TABLE).select("*").eq("token", token).limit(1).execute()
    row = (r.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="token_not_found")

    # если уже оплачено — не создаём заново
    if row.get("payment_status") == "paid":
        return {"ok": True, "already_paid": True, "checkout_url": None}

    # 2) создаём Checkout Session
    # ВАЖНО: metadata — чтобы в webhook понять, что именно оплатили
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=STRIPE_SUCCESS_URL or "https://t.me/",
        cancel_url=STRIPE_CANCEL_URL or "https://t.me/",
        metadata={
            "token": token,
            "tg_chat_id": str(row.get("tg_chat_id") or ""),
        },
    )

    # 3) сохраняем session_id
    sb.table(TOKENS_TABLE).update(
        {"stripe_session_id": session.id, "payment_status": "pending"}
    ).eq("token", token).execute()

    return {"ok": True, "checkout_url": session.url, "session_id": session.id}

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

    # Нас интересует успешная оплата
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        token = (session.get("metadata") or {}).get("token")
        tg_chat_id = (session.get("metadata") or {}).get("tg_chat_id")

        if token:
            # отмечаем оплату
            sb.table(TOKENS_TABLE).update(
                {
                    "payment_status": "paid",
                    "paid_at": utcnow_iso(),
                    "stripe_session_id": session.get("id"),
                    "stripe_customer_email": (session.get("customer_details") or {}).get("email"),
                    "status": "paid",  # можно держать отдельно от completed, как хочешь
                }
            ).eq("token", token).execute()

        # отправляем пользователю сообщение в TG
        if tg_chat_id:
            # тут ты даёшь ссылку на платформу (например APP_URL + /?token=... или отдельный доступ)
            # пока сделаем просто "переход в платформу"
            platform_link = APP_URL  # лучше потом сделать персональный доступ
            buttons = [
                [{"text": "💠 Открыть платформу", "url": platform_link}],
            ]
            if TG_GROUP_INVITE_LINK:
                buttons.append([{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}])

            tg_send(
                str(tg_chat_id),
                "✅ Оплата прошла! Вот ваши материалы и доступ:",
                buttons=buttons,
            )

    return JSONResponse({"ok": True})

# -------------------------
# Telegram webhook handler
# -------------------------
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    try:
        data = await req.json()
        msg = data.get("message") or data.get("callback_query", {}).get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")

        # Callback button pressed
        cb = data.get("callback_query")
        if cb and chat_id:
            action = cb.get("data")
            if action == "start_diag":
                link = issue_link(chat_id, source="tg")
                tg_send(
                    chat_id,
                    "Отлично! Вот твоя персональная ссылка на диагностику 👇",
                    buttons=[[{"text": "🚀 Начать диагностику", "url": link}]],
                )
            return {"ok": True}

        # Text message
        text = (data.get("message", {}) or {}).get("text", "") or ""
        if not chat_id:
            return {"ok": True}

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else ""

            # если пришли из диагностики — payload это token
            if start_payload:
                token = start_payload

                # сохраняем, что этот chat_id привязан к token (на всякий)
                sb.table(TOKENS_TABLE).update(
                    {"tg_chat_id": chat_id}
                ).eq("token", token).execute()

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

    # обычный старт (без payload)
    tg_send(
        chat_id,
        "Привет! Нажми кнопку — я выдам персональную ссылку.",
        buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
    )
    return {"ok": True}

        return {"ok": True}

    except Exception as e:
        # чтобы webhook не падал молча
        raise HTTPException(status_code=500, detail=f"webhook_error: {e}")


# -------------------------
# Complete from Streamlit
# -------------------------
class CompleteIn(BaseModel):
    token: str
    session_id: str
    client_name: str | None = "Клиент"


@app.post("/complete")
def complete(inp: CompleteIn):
    try:
        r = (
            sb.table(TOKENS_TABLE)
            .select("*")
            .eq("token", inp.token)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return {"ok": False, "err": "token_not_found"}

        row = rows[0]

        sb.table(TOKENS_TABLE).update(
            {
                "status": "completed",
                "completed_at": utcnow_iso(),
                "session_id": inp.session_id,
            }
        ).eq("token", inp.token).execute()

        chat_id = row.get("tg_chat_id")
        if not chat_id:
            return {"ok": False, "err": "tg_chat_id_missing"}

        
        # создаём checkout-ссылку на оплату
        checkout_url = None
        try:
            # создаём оплату по токену
            # ВАЖНО: inp.token — это тот же token
            resp = requests.post(
                f"{os.getenv('API_BASE_URL','')}/pay/create-checkout",
                params={"token": inp.token},
                timeout=12,
            )
            if resp.status_code == 200:
                checkout_url = (resp.json() or {}).get("checkout_url")
        except Exception:
            checkout_url = None
        
        buttons = []

        # кнопка оплаты
        if checkout_url:
            buttons.append(
                [{"text": "💳 Получить расширенный доступ за $19", "url": checkout_url}]
            )

        # клуб (опционально)
        if TG_GROUP_INVITE_LINK:
            buttons.append(
                [{"text": "👥 Войти в клуб", "url": TG_GROUP_INVITE_LINK}]
            )
        
        tg_send(
            str(chat_id),
            f"✅ {inp.client_name or 'Готово'}! Диагностика завершена.\n\nВыбирай следующий шаг:",
            buttons=buttons,
        )
        
        
        return {"ok": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"complete_error: {e}")