"""
server.py — FastAPI webhook receiver (llamado por el Cloudflare Worker)

Uso:
    uvicorn src.server:app --host 0.0.0.0 --port 8000
    uvicorn src.server:app --reload          # desarrollo local
"""
import os
import re
import tempfile
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone
from pathlib import Path

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
                        media_ref = row.get("media_ref")

                        # Transcribir audio automáticamente
                        if row["type"] == "audio" and media_ref and META_USER_TOKEN:
                            transcription = _transcribe_audio(media_ref)
                            if transcription:
                                row["content"] = transcription
                                row["needs_review"] = 0
                                print(f"[audio] Transcrito: {transcription[:80]}")
                            else:
                                row["needs_review"] = 1
                                print(f"[audio] Sin transcripción — marcado needs_review=1")

                        # Guardar imágenes y documentos antes de que expiren en Meta
                        if row["type"] in ("image", "doc") and media_ref and META_USER_TOKEN:
                            media_bytes, ext = _fetch_meta_media(media_ref)
                            if media_bytes:
                                row["media_path"] = _save_media(media_bytes, ext, media_ref)

                        # Extraer monto de mensajes de texto (regla Henry B8-c)
                        if row["type"] == "text" and row.get("content"):
                            amount, multi = _extract_amount(row["content"])
                            row["amount"] = amount
                            if multi:
                                row["needs_review"] = 1
                                print(f"[amount] Múltiples montos → needs_review=1: {row['content'][:60]}")
                            elif amount is not None:
                                print(f"[amount] Extraído: ${amount}")

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
        SELECT message_id, sender, content, type, media_ref, media_path, amount, source, needs_review, created_at
        FROM messages ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    conn.close()
    return JSONResponse(
        content={"count": len(rows), "messages": [dict(r) for r in rows]},
        media_type="application/json; charset=utf-8",
    )


# ── Descarga de media desde Meta ─────────────────────────────────────────────

_MIME_EXT = {
    "image/jpeg":      ".jpg",
    "image/png":       ".png",
    "image/webp":      ".webp",
    "application/pdf": ".pdf",
    "audio/ogg":       ".ogg",
    "audio/mpeg":      ".mp3",
}


def _fetch_meta_media(media_id: str) -> tuple[bytes, str] | tuple[None, None]:
    """
    Descarga un archivo de Meta Graph API.
    Devuelve (bytes, extensión) o (None, None) si falla.
    """
    if not META_USER_TOKEN:
        return None, None
    try:
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {META_USER_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            meta_info = json.loads(r.read())

        download_url = meta_info.get("url")
        if not download_url:
            return None, None

        ext = _MIME_EXT.get(meta_info.get("mime_type", ""), ".bin")

        req2 = urllib.request.Request(
            download_url,
            headers={"Authorization": f"Bearer {META_USER_TOKEN}"}
        )
        with urllib.request.urlopen(req2, timeout=30) as r:
            return r.read(), ext

    except Exception as e:
        print(f"[media] Error descargando {media_id}: {e}")
        return None, None


def _save_media(media_bytes: bytes, ext: str, media_id: str) -> str | None:
    """
    Guarda el archivo en el volumen Railway (/data/media/).
    Devuelve la ruta local o None si falla.
    """
    try:
        media_dir = Path(DB_PATH).parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        dest = media_dir / f"{media_id}{ext}"
        dest.write_bytes(media_bytes)
        print(f"[media] Guardado: {dest} ({len(media_bytes):,} bytes)")
        return str(dest)
    except Exception as e:
        print(f"[media] Error guardando {media_id}: {e}")
        return None


# ── Transcripción de audio ────────────────────────────────────────────────────

def _transcribe_audio(media_id: str) -> str | None:
    """
    Descarga el audio de Meta y transcribe con Groq (Whisper large-v3).
    Devuelve el texto o None si falla.
    """
    if not GROQ_API_KEY:
        print("[audio] GROQ_API_KEY no configurada — audio sin transcribir")
        return None

    media_bytes, _ = _fetch_meta_media(media_id)
    if not media_bytes:
        return None

    ogg_path = None
    try:
        from groq import Groq
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(media_bytes)
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


# ── Extracción de montos ─────────────────────────────────────────────────────

# Captura formatos salvadoreños: $29.40  $29. 40  $1,197.00  $150  $ 150
_AMOUNT_RE = re.compile(r'\$\s*[\d,]+(?:\.\s*\d+)?')


def _extract_amount(text: str) -> tuple[float | None, bool]:
    """
    Regla Henry (B8-c): 1 monto → (valor, False) | >1 → (None, True) | 0 → (None, False).
    Nunca entra un número dudoso en la suma de gastos por obra.
    """
    matches = _AMOUNT_RE.findall(text)
    if len(matches) == 1:
        try:
            return float(re.sub(r'[\s,]', '', matches[0].replace('$', ''))), False
        except ValueError:
            return None, False
    return (None, len(matches) > 1)


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
        "message_id":  msg_id,
        "line":        LINE_NAME,
        "chat_ref":    chat_ref,
        "sender":      from_num,
        "timestamp":   ts,
        "type":        msg_type,
        "content":     content,
        "media_ref":   media_ref,
        "media_path":  None,
        "amount":      None,
        "source":      "webhook",
        "needs_review": 0,
    }


def _upsert(conn, row: dict) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO messages
          (message_id, line, chat_ref, sender, timestamp,
           type, content, media_ref, media_path, amount, source, needs_review)
        VALUES (:message_id,:line,:chat_ref,:sender,:timestamp,
                :type,:content,:media_ref,:media_path,:amount,:source,:needs_review)
    """, row)
    conn.commit()
