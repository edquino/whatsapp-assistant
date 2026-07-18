"""
server.py — FastAPI webhook receiver (llamado por el Cloudflare Worker)

Uso:
    uvicorn src.server:app --host 0.0.0.0 --port 8000
    uvicorn src.server:app --reload          # desarrollo local
"""
import hashlib
import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv

from src.db import get_connection, init_schema

load_dotenv()

app = FastAPI(title="WhatsApp Assistant — Ingest Server")

BACKEND_SECRET  = os.getenv("BACKEND_SECRET", "")
LINE_NAME       = os.getenv("LINE_NAME", "maurisito")
DB_PATH         = os.getenv("DB_PATH", "data/whatsapp.db")


@app.on_event("startup")
def startup():
    conn = get_connection(DB_PATH)
    init_schema(conn)
    conn.close()


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe el payload raw de Meta, reenviado por el Cloudflare Worker."""
    # Validar secret del Worker
    if BACKEND_SECRET:
        if request.headers.get("x-worker-secret") != BACKEND_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()

    conn = get_connection(DB_PATH)
    inserted = 0
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    row = _normalize(msg, value)
                    if row:
                        _upsert(conn, row)
                        inserted += 1
    finally:
        conn.close()

    return {"status": "ok", "inserted": inserted}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Normalización de payload Meta → schema messages ──────────────────────────

def _normalize(msg: dict, value: dict) -> dict | None:
    msg_id   = msg.get("id")
    ts_epoch = msg.get("timestamp")
    from_num = msg.get("from")
    msg_type = msg.get("type", "text")

    if not msg_id or not ts_epoch:
        return None

    ts = datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc).isoformat()

    content   = None
    media_ref = None

    if msg_type == "text":
        content = msg.get("text", {}).get("body")
    elif msg_type == "image":
        media_ref = msg.get("image", {}).get("id")
        content   = msg.get("image", {}).get("caption")
    elif msg_type == "audio":
        media_ref = msg.get("audio", {}).get("id")
        msg_type  = "audio"
    elif msg_type == "document":
        media_ref = msg.get("document", {}).get("id")
        content   = msg.get("document", {}).get("filename")
        msg_type  = "doc"
    elif msg_type == "video":
        media_ref = msg.get("video", {}).get("id")
        msg_type  = "video"

    # Obtener número del asistente (WABA) desde metadata
    waba_id = value.get("metadata", {}).get("phone_number_id", "waba")
    chat_ref = f"dm:{from_num}"

    return {
        "message_id": msg_id,
        "line":        LINE_NAME,
        "chat_ref":    chat_ref,
        "sender":      from_num,
        "timestamp":   ts,
        "type":        msg_type,
        "content":     content,
        "media_ref":   media_ref,
        "source":      "webhook",
        "needs_review": 0,
    }


def _upsert(conn, row: dict) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO messages
          (message_id, line, chat_ref, sender, timestamp,
           type, content, media_ref, source, needs_review)
        VALUES (:message_id,:line,:chat_ref,:sender,:timestamp,
                :type,:content,:media_ref,:source,:needs_review)
    """, row)
    conn.commit()
