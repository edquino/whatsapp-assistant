"""
server.py — FastAPI webhook receiver (llamado por el Cloudflare Worker)

Uso:
    uvicorn src.server:app --host 0.0.0.0 --port 8000
    uvicorn src.server:app --reload          # desarrollo local
"""
import os
import subprocess
import tempfile
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv

from src.db import get_connection, init_schema

load_dotenv()

app = FastAPI(title="WhatsApp Assistant — Ingest Server")

BACKEND_SECRET  = os.getenv("BACKEND_SECRET", "")
LINE_NAME       = os.getenv("LINE_NAME", "maurisito")
DB_PATH         = os.getenv("DB_PATH", "data/whatsapp.db")
META_USER_TOKEN = os.getenv("META_USER_TOKEN", "")


@app.on_event("startup")
def startup():
    conn = get_connection(DB_PATH)
    init_schema(conn)
    conn.close()


@app.post("/ingest")
async def ingest(request: Request):
    """Recibe el payload raw de Meta, reenviado por el Cloudflare Worker."""
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
        "token_preview": META_USER_TOKEN[:12] + "..." if META_USER_TOKEN else "NOT SET"
    }


@app.get("/recent")
def recent():
    conn = get_connection(DB_PATH)
    rows = conn.execute("""
        SELECT message_id, sender, content, type, media_ref, source, needs_review, created_at
        FROM messages ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    conn.close()
    return {"count": len(rows), "messages": [dict(r) for r in rows]}


# ── Transcripción de audio ────────────────────────────────────────────────────

def _transcribe_audio(media_id: str) -> str | None:
    """
    Descarga el audio de Meta, convierte a WAV con ffmpeg,
    y transcribe con Google Speech Recognition.
    Devuelve el texto o None si falla.
    """
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

        # 3. Guardar en archivo temporal
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg_f:
            ogg_f.write(audio_bytes)
            ogg_path = ogg_f.name

        wav_path = ogg_path.replace(".ogg", ".wav")

        # 4. Convertir a WAV 16kHz mono con ffmpeg
        result = subprocess.run(
            ["ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1", "-y", wav_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            return None

        # 5. Transcribir con Google Speech Recognition
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        text = recognizer.recognize_google(audio_data, language="es-SV")

        return text

    except Exception as e:
        print(f"[audio] Error transcripción: {e}")
        return None

    finally:
        # Limpiar archivos temporales
        for path in [ogg_path if 'ogg_path' in dir() else None,
                     wav_path if 'wav_path' in dir() else None]:
            if path:
                try:
                    os.unlink(path)
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
