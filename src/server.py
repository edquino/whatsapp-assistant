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

app = FastAPI(title="WhatsApp Assistant — Ingest Server")  # redeploy

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


@app.get("/test-transcribe/{media_id}")
def test_transcribe(media_id: str):
    """Diagnóstico: intenta transcribir un media_id y devuelve el resultado o error."""
    import subprocess, tempfile, urllib.request, json as _json
    steps = {}
    try:
        # Step 1: ffmpeg disponible via imageio-ffmpeg?
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_bin = get_ffmpeg_exe()
        r = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, timeout=5)
        steps["ffmpeg"] = f"ok ({ffmpeg_bin})" if r.returncode == 0 else f"error: {r.stderr[:100]}"
    except Exception as e:
        steps["ffmpeg"] = f"not found: {e}"

    try:
        # Step 2: Meta token funciona?
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {META_USER_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            meta = _json.loads(resp.read())
        steps["meta_api"] = "ok"
        steps["audio_url"] = meta.get("url", "no url")[:60] + "..."
        steps["mime_type"] = meta.get("mime_type")
    except Exception as e:
        steps["meta_api"] = f"error: {e}"
        return {"steps": steps}

    # Step 3: Whisper disponible?
    try:
        import whisper
        steps["whisper_import"] = "ok"
    except Exception as e:
        steps["whisper_import"] = f"error: {e}"
        return {"steps": steps}

    try:
        model = whisper.load_model("tiny")
        steps["whisper_load_model"] = "ok"
    except Exception as e:
        steps["whisper_load_model"] = f"error: {e}"
        return {"steps": steps}

    # Step 4: Transcribir
    try:
        import tempfile, urllib.request as _ur
        req3 = _ur.Request(steps["audio_url"].rstrip("...") + "PLACEHOLDER", headers={"Authorization": f"Bearer {META_USER_TOKEN}"})
        # re-fetch audio_url completa
        req_meta = _ur.Request(f"https://graph.facebook.com/v19.0/{media_id}", headers={"Authorization": f"Bearer {META_USER_TOKEN}"})
        with _ur.urlopen(req_meta, timeout=10) as resp:
            import json as _j
            meta_full = _j.loads(resp.read())
        audio_url_full = meta_full.get("url")
        req_audio = _ur.Request(audio_url_full, headers={"Authorization": f"Bearer {META_USER_TOKEN}"})
        with _ur.urlopen(req_audio, timeout=15) as resp:
            audio_bytes = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            ogg_path = f.name
        wav_path = ogg_path.replace(".ogg", ".wav")
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_bin = get_ffmpeg_exe()
        import subprocess as _sp
        r = _sp.run([ffmpeg_bin, "-i", ogg_path, "-ar", "16000", "-ac", "1", "-y", wav_path], capture_output=True, timeout=30)
        steps["ffmpeg_convert"] = "ok" if r.returncode == 0 else f"error: {r.stderr.decode()[:200]}"
        if r.returncode != 0:
            return {"steps": steps}
        import wave, numpy as np
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        audio_np = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
        result = model.transcribe(audio_np, language="es", fp16=False)
        text = result["text"].strip()
        steps["transcription"] = text
        return {"steps": steps, "transcription": text}
    except Exception as e:
        import traceback
        steps["transcription_error"] = traceback.format_exc()
        return {"steps": steps}


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
    y transcribe con Whisper local (modelo base, CPU).
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

        # 4. Convertir a WAV 16kHz mono con ffmpeg (via imageio-ffmpeg, sin depender del sistema)
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_bin = get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg_bin, "-i", ogg_path, "-ar", "16000", "-ac", "1", "-y", wav_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            return None

        # 5. Transcribir con Whisper local — cargar WAV como numpy para evitar
        # que Whisper llame a ffmpeg del sistema (no disponible en Railway)
        import whisper, wave, numpy as np
        _model = whisper.load_model("tiny")
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        audio_np = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
        result = _model.transcribe(audio_np, language="es", fp16=False)
        text = result["text"].strip()

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
