import os
import secrets
from datetime import datetime, timezone

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

        if text in ("/start", "start", "Start", "начать", "Начать"):
            tg_send(
                chat_id,
                "Привет! Я бот диагностики Personal Potentials.\n\nНажми кнопку — я выдам персональную ссылку.",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )
        else:
            tg_send(
                chat_id,
                "Нажми «✨ Начать», и я выдам персональную ссылку.",
                buttons=[[{"text": "✨ Начать", "callback_data": "start_diag"}]],
            )

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

        buttons = [[{"text": "🔥 Войти в группу разбора", "url": TG_GROUP_INVITE_LINK}]]
        if PAY_URL:
            buttons.append([{"text": "💎 Платная консультация", "url": PAY_URL}])

        tg_send(
            str(chat_id),
            f"✅ {inp.client_name or 'Готово'}! Диагностика завершена.\n\nВыбирай следующий шаг:",
            buttons=buttons,
        )

        return {"ok": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"complete_error: {e}")