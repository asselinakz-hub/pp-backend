import os, secrets
import requests
from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]

APP_URL = os.environ["APP_URL"]  # твой Streamlit URL, без / в конце
TG_GROUP_INVITE_LINK = os.environ["TG_GROUP_INVITE_LINK"]  # одна приватная invite-ссылка
PAY_URL = os.environ.get("PAY_URL", "")  # можно пусто на MVP

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

def tg_send(chat_id: str, text: str, buttons=None):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=12
    )

def issue_link(chat_id: str, source="tg", campaign=""):
    token = secrets.token_urlsafe(16)
    sb.table("link_tokens").insert({
        "token": token,
        "tg_chat_id": str(chat_id),
        "source": source,
        "campaign": campaign,
    }).execute()
    return f"{APP_URL}/?t={token}"

@app.get("/health")
def health():
    return {"ok": True}

# -------- Telegram webhook handler --------
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
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
                buttons=[[{"text":"🚀 Начать диагностику", "url": link}]]
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
            buttons=[[{"text":"✨ Начать", "callback_data":"start_diag"}]]
        )
    else:
        tg_send(
            chat_id,
            "Нажми «✨ Начать», и я выдам персональную ссылку.",
            buttons=[[{"text":"✨ Начать", "callback_data":"start_diag"}]]
        )

    return {"ok": True}

# -------- Complete from Streamlit --------
class CompleteIn(BaseModel):
    token: str
    session_id: str
    client_name: str | None = "Клиент"

@app.post("/complete")
def complete(inp: CompleteIn):
    row = sb.table("link_tokens").select("*").eq("token", inp.token).single().execute().data
    if not row:
        return {"ok": False, "err": "token_not_found"}

    sb.table("link_tokens").update({
        "completed_at": "now()",
        "session_id": inp.session_id,
    }).eq("token", inp.token).execute()

    chat_id = row["tg_chat_id"]

    buttons = [
        [{"text": "🔥 Войти в группу разбора", "url": TG_GROUP_INVITE_LINK}],
    ]
    if PAY_URL:
        buttons.append([{"text": "💎 Платная консультация", "url": PAY_URL}])

    tg_send(
        chat_id,
        f"✅ {inp.client_name or 'Готово'}! Диагностика завершена.\n\nВыбирай следующий шаг:",
        buttons=buttons
    )
    return {"ok": True}
    
from fastapi import FastAPI, HTTPException
from supabase import create_client
import os

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # важно: service role
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

@app.get("/api/token/{token}")
def get_token(token: str):
    r = supabase.table("tokens").select("token,status,completed_at,telegram_chat_id").eq("token", token).limit(1).execute()
    rows = (r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="token_not_found")
    return rows[0]
