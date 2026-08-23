"""
server.py — FastAPI webhook receiver (llamado por el Cloudflare Worker)

Uso:
    uvicorn src.server:app --host 0.0.0.0 --port 8000
    uvicorn src.server:app --reload          # desarrollo local
"""
import os
import tempfile
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from src.db import get_connection, init_schema

load_dotenv()

app = FastAPI(title="WhatsApp Assistant — Ingest Server")

BACKEND_SECRET  = os.getenv("BACKEND_SECRET", "")
LINE_NAME       = os.getenv("LINE_NAME", "maurisito")
DB_PATH         = os.getenv("DB_PATH", "data/whatsapp.db")
META_USER_TOKEN = os.getenv("META_USER_TOKEN", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")


@app.on_event("startup")
def startup():
    conn = get_connection(DB_PATH)
    init_schema(conn)
    conn.close()
    print(f"[startup] Groq transcription {'habilitada' if GROQ_API_KEY else 'DESHABILITADA — falta GROQ_API_KEY'}.")


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe el payload raw de Meta, reenviado por el Cloudflare Worker."""
    if not BACKEND_SECRET or request.headers.get("x-worker-secret") != BACKEND_SECRET:
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
                        # Transcribir audio automáticamente si hay token
                        if row["type"] == "audio" and row.get("media_ref") and META_USER_TOKEN:
                            transcription = _transcribe_audio(row["media_ref"])
                            if transcription:
                                row["content"] = transcription
                                row["needs_review"] = 0
                                print(f"[audio] Transcrito: {transcription[:80]}")
                            else:
                                row["needs_review"] = 1
                                print(f"[audio] Sin transcripción — marcado needs_review=1")
                        _upsert(conn, row)
                        inserted += 1
    finally:
        conn.close()

    return {"status": "ok", "inserted": inserted}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "meta_token_set": bool(META_USER_TOKEN),
        "groq_api_set": bool(GROQ_API_KEY),
    }


@app.get("/recent")
def recent(request: Request):
    if not BACKEND_SECRET or request.headers.get("x-worker-secret") != BACKEND_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    conn = get_connection(DB_PATH)
    rows = conn.execute("""
        SELECT message_id, sender, content, type, media_ref, source, needs_review, created_at
        FROM messages ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    conn.close()
    return JSONResponse(
        content={"count": len(rows), "messages": [dict(r) for r in rows]},
        media_type="application/json; charset=utf-8",
    )


# ── Transcripción de audio ────────────────────────────────────────────────────

def _transcribe_audio(media_id: str) -> str | None:
    """
    Descarga el audio de Meta y transcribe con Groq (Whisper large-v3).
    Devuelve el texto o None si falla.
    """
    if not GROQ_API_KEY:
        print("[audio] GROQ_API_KEY no configurada — audio sin transcribir")
        return None

    ogg_path = None
    try:
        # 1. Obtener URL de descarga desde Meta Graph API
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {META_USER_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        audio_url = meta.get("url")
        if not audio_url:
            return None

        # 2. Descargar el archivo OGG/Opus
        req2 = urllib.request.Request(
            audio_url,
            headers={"Authorization": f"Bearer {META_USER_TOKEN}"}
        )
        with urllib.request.urlopen(req2, timeout=15) as r:
            audio_bytes = r.read()

        # 3. Transcribir con Groq (Whisper large-v3) — sin ffmpeg, sin modelo local
        from groq import Groq
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            ogg_path = f.name

        client = Groq(api_key=GROQ_API_KEY)
        with open(ogg_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=("audio.ogg", f, "audio/ogg"),
                language="es"
            )
        return result.text.strip()

    except Exception as e:
        print(f"[audio] Error transcripción: {e}")
        return None

    finally:
        if ogg_path:
            try:
                os.unlink(ogg_path)
            except Exception:
                pass


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
    elif msg_type == "document":
        media_ref = msg.get("document", {}).get("id")
        content   = msg.get("document", {}).get("filename")
        msg_type  = "doc"
    elif msg_type == "video":
        media_ref = msg.get("video", {}).get("id")

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
