# whatsapp-assistant

Sistema para capturar y normalizar mensajes de WhatsApp en una base de datos.  
Construido para Edge AI Growth LLC — Instancia 1: Maurisito (Piscinas El Pacífico).

---

## Estado de producción — agosto 2026

| Componente | URL | Estado |
|-----------|-----|--------|
| FastAPI backend | `whatsapp-assistant-production-6e4f.up.railway.app` | ✅ Online |
| Cloudflare Worker | `whatsapp-assistant-webhook.edquino.workers.dev/webhook` | ✅ Online |
| Base de datos | Volumen Railway `/data/whatsapp.db` | ✅ Persistente |

---

## Puntos cerrados

### P1 — Parser de exports (100% ✅)

Importa chats exportados de WhatsApp (`.zip`) y extrae todos los gastos:

| Formato | Método | Estado |
|---------|--------|--------|
| Texto con monto (`$29.40 de cal`) | Regex | ✅ |
| Foto de recibo o transferencia | Claude Vision (`claude-haiku-4-5`) | ✅ |
| DTE / factura PDF | `pdfplumber` | ✅ |
| Nota de voz `.ogg/.opus` | Groq API `whisper-large-v3` | ✅ |
| Deduplicación (re-import = 0 duplicados) | Hash por `message_id` | ✅ |
| Flag `needs_review=1` (baja confianza) | — | ✅ |

**Resultado de validación contra fixture real:**

| Grupo | Mensajes | Total extraído | Referencia manual | Delta |
|-------|----------|---------------|-------------------|-------|
| El Encanto | 14 | $1,197.33 | $1,197.00 | $0.33 ✅ |
| Piscina Ilobasco | 23 | $0 | — | Chat de specs, sin gastos |

```bash
python src/parse_export.py "ruta/al/export.zip" --line maurisito
```

---

### P3 — Webhook WABA (100% ✅)

Recepción de mensajes en tiempo real desde WhatsApp Cloud API:

- ✅ Cloudflare Worker — responde a Meta en <20ms, firma HMAC-SHA256 validada
- ✅ FastAPI en Railway — procesa y guarda en DB
- ✅ Volumen Railway — datos persisten a través de redeploys (`DB_PATH=/data/whatsapp.db`)
- ✅ Cadena completa validada: WhatsApp → Worker → Railway → DB en <2 segundos
- ✅ Transcripción automática de audios al llegar por webhook (Groq, sin costo adicional por instancia)

---

### P1.4 — Transcripción de voz (100% ✅)

Cambio aplicado el 22-ago-2026: reemplazado Whisper local por **Groq API (whisper-large-v3)**.

- Costo: ~$0.11/hora de audio ≈ $1–3/mes al volumen real de la obra
- Precisión: large-v3, mejor disponible de la familia Whisper
- Sin carga de modelo en RAM — sin problema de tamaño de instancia Railway
- Variable requerida en Railway: `GROQ_API_KEY`

---

### E42 — Seguridad (100% ✅)

Tres puntos cerrados antes de conectar datos reales de Maurisito:

| Endpoint | Problema | Fix aplicado |
|----------|----------|-------------|
| `/health` | Exponía primeros 12 chars del token Meta | Reemplazado por `bool(META_USER_TOKEN)` |
| `/test-transcribe/{id}` | Abierto — cualquiera bajaba media con el token del servidor | Endpoint eliminado |
| `/recent` | Sin auth — devolvía mensajes a cualquiera | Fail-closed: requiere `x-worker-secret` siempre |
| `/ingest` | Fail-open si `BACKEND_SECRET` vacía | Fail-closed: si no está configurada, rechaza todo |

---

## Arquitectura

```
WhatsApp (Meta Cloud API)
        │
        ▼
Cloudflare Worker
  • Recibe POST de Meta
  • Valida firma HMAC-SHA256 (META_APP_SECRET)
  • Responde 200 OK a Meta en <20ms
  • Reenvía al backend con X-Worker-Secret
        │
        ▼
FastAPI (Railway)
  • Valida X-Worker-Secret (BACKEND_SECRET)
  • Normaliza payload Meta → schema messages
  • Transcribe audios → Groq API
  • Extrae montos de imágenes → Claude Vision
  • Inserta en SQLite (volumen Railway)
        │
        ▼
SQLite — tabla messages
  (volumen persistente /data/whatsapp.db)
```

---

## Setup desde cero

### 1. Clonar y configurar

```bash
git clone https://github.com/edquino/whatsapp-assistant.git
cd whatsapp-assistant
cp .env.example .env
# Completar .env con las credenciales
```

### 2. Instalar dependencias Python

```bash
pip install -r requirements.txt
```

### 3. Levantar servidor local

```bash
uvicorn src.server:app --reload
# Health check: http://localhost:8000/health
```

### 4. Deploy Cloudflare Worker

```bash
cd worker
wrangler login
wrangler secret put META_VERIFY_TOKEN   # token que elegiste
wrangler secret put META_APP_SECRET     # App Secret de Meta for Developers
wrangler secret put BACKEND_URL         # https://tu-app.railway.app
wrangler secret put BACKEND_SECRET      # secret compartido Worker ↔ Railway
wrangler deploy
```

### 5. Registrar webhook en Meta

Meta for Developers → App → WhatsApp → Webhooks:

- **Callback URL:** `https://whatsapp-assistant-webhook.<cuenta>.workers.dev/webhook`
- **Verify Token:** el mismo que pusiste en `META_VERIFY_TOKEN`
- **Suscribir a:** `messages`

### 6. Variables Railway (producción)

| Variable | Descripción |
|----------|-------------|
| `BACKEND_SECRET` | Secret compartido con el Cloudflare Worker |
| `LINE_NAME` | Identificador de instancia (ej: `maurisito`) |
| `DB_PATH` | `/data/whatsapp.db` (volumen Railway) |
| `META_USER_TOKEN` | Token de acceso de Meta (System User o temporal 24h) |
| `GROQ_API_KEY` | API key de console.groq.com para transcripción de audio |

---

## Verificar mensajes recibidos

```powershell
$response = Invoke-WebRequest `
    -Uri "https://whatsapp-assistant-production-6e4f.up.railway.app/recent" `
    -Headers @{"x-worker-secret"="TU_BACKEND_SECRET"} `
    -UseBasicParsing
[System.Text.Encoding]::UTF8.GetString($response.RawContentBytes)
```

> Usar `[System.Text.Encoding]::UTF8.GetString($response.RawContentBytes)` para que PowerShell 5.1 lea la respuesta como UTF-8 y los acentos aparezcan correctamente.

---

## Importar exports de WhatsApp

```bash
python src/parse_export.py "ruta/al/export.zip" --line maurisito
```

---

## Estructura del repo

```
src/
  server.py         — FastAPI: recibe webhooks, transcribe audio, guarda en DB
  parse_export.py   — P1: importa exports .zip de WhatsApp
  db.py             — helpers SQLite
worker/
  index.js          — Cloudflare Worker: receptor de Meta
  wrangler.toml     — config del Worker
```

---

## Roadmap

| Fase | Estado | Descripción |
|------|--------|-------------|
| Fase 2 — Sandbox WhatsApp | ✅ Completa | Parser, webhook, Whisper/Groq, volumen, seguridad |
| Fase 3 — Reader vivo | ⏳ Bloqueado — Meta B2 | Conectar al WhatsApp real de Maurisito |
| Fase 4 — Control Financiero | ⬜ Pendiente | Reportes automáticos por obra |
| Fase 5 — Sistema de Obra | ⬜ Pendiente | Grupos v2, dashboards, alertas |

**Bloqueador activo:** Meta Business Verification (B2) — en revisión, 2–14 días hábiles. Una vez aprobada: 15 minutos para activar el sistema con Maurisito.

---

*Edge AI Growth LLC — Uso interno*
