# whatsapp-assistant

Sistema instalable para capturar y normalizar mensajes de WhatsApp en una base de datos local.

Construido para Edge AI Growth LLC. Instancia 1: Maurisito (Piscinas El Pacífico).

---

## Arquitectura

```
Meta Cloud API
      │
      ▼
Cloudflare Worker  ← recibe webhooks, valida firma, responde 200 a Meta
      │
      ▼
FastAPI server     ← normaliza payload, inserta en DB
      │
      ▼
SQLite / PostgreSQL ← tabla `messages` (contrato de interfaz)
```

Para leer grupos existentes (gastos, obras): export manual → `parse_export.py`.

---

## Setup en menos de 30 minutos

### 1. Clonar y configurar

```bash
git clone https://github.com/edge-ai-growth/whatsapp-assistant.git
cd whatsapp-assistant
cp .env.example .env
# Editar .env con tus credenciales
```

### 2. Instalar dependencias Python

```bash
pip install -r requirements.txt
```

### 3. Levantar el servidor local (desarrollo)

```bash
uvicorn src.server:app --reload
```

El servidor queda en `http://localhost:8000`. Verificar con: `curl http://localhost:8000/health`

### 4. Exponer el servidor con ngrok (solo para dev)

```bash
ngrok http 8000
# Copia la URL https://xxxx.ngrok.io — la usarás como BACKEND_URL del Worker
```

### 5. Deploy del Cloudflare Worker

```bash
cd worker
npm install -g wrangler    # solo la primera vez
wrangler login

# Configurar secretos (una vez):
wrangler secret put META_VERIFY_TOKEN
wrangler secret put META_APP_SECRET
wrangler secret put BACKEND_URL        # https://xxxx.ngrok.io o URL de producción
wrangler secret put BACKEND_SECRET

# Deploy:
wrangler deploy
```

El Worker queda en `https://whatsapp-assistant-webhook.<tu-cuenta>.workers.dev/webhook`

### 6. Registrar el webhook en Meta

1. Meta for Developers → tu App → WhatsApp → Configuración
2. Webhook URL: `https://whatsapp-assistant-webhook.<tu-cuenta>.workers.dev/webhook`
3. Verify Token: el mismo que pusiste en `META_VERIFY_TOKEN`
4. Suscribirse a: `messages`

### 7. Importar exports de grupos existentes

```bash
python src/parse_export.py "ruta/al/export.zip" --line maurisito
```

---

## Variables de entorno

Ver `.env.example` para la lista completa con descripciones.

---

## Estructura del repo

```
src/
  parse_export.py   — P1: importar exports .zip de WhatsApp
  db.py             — helpers de base de datos
  server.py         — FastAPI: recibe webhooks del Worker
worker/
  index.js          — Cloudflare Worker: receiver de Meta
  wrangler.toml     — configuración del Worker
docs/
  architecture.md   — decisiones de diseño y restricciones Meta
```

---

## Pruebas de aceptación (P1–P3)

| Prueba | Comando | Pasa cuando |
|--------|---------|-------------|
| P1 Export | `python src/parse_export.py <zip> --dry-run` | Mensajes parseados, 0 duplicados en re-import |
| P3 Webhook | Mensaje al WABA | Fila aparece en DB en <5 seg |

---

## Licencia

Uso interno — Edge AI Growth LLC.
