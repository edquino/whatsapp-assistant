/**
 * Cloudflare Worker — WhatsApp Webhook Receiver
 *
 * Responsabilidades:
 *   GET  /webhook  → verificación de webhook con Meta (hub challenge)
 *   POST /webhook  → recibe mensajes entrantes, valida firma, reenvía al backend Python
 *
 * Variables de entorno (wrangler secret put / .dev.vars):
 *   META_VERIFY_TOKEN   — el token que elegiste al registrar el webhook en Meta
 *   META_APP_SECRET     — App Secret para validar la firma X-Hub-Signature-256
 *   BACKEND_URL         — URL del servidor FastAPI (ej: https://tu-app.railway.app)
 *   BACKEND_SECRET      — Header secreto para que el backend acepte solo al Worker
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname !== "/webhook") {
      return new Response("Not found", { status: 404 });
    }

    if (request.method === "GET") {
      return handleVerification(request, env);
    }

    if (request.method === "POST") {
      return handleMessage(request, env, ctx);
    }

    return new Response("Method not allowed", { status: 405 });
  },
};

// ── Verificación de webhook (paso único al registrar en Meta) ─────────────────

function handleVerification(request, env) {
  const url = new URL(request.url);
  const mode      = url.searchParams.get("hub.mode");
  const token     = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode === "subscribe" && token === env.META_VERIFY_TOKEN) {
    console.log("Webhook verificado por Meta");
    return new Response(challenge, { status: 200 });
  }

  console.error("Verificacion fallida — token incorrecto o mode invalido");
  return new Response("Forbidden", { status: 403 });
}

// ── Mensajes entrantes ────────────────────────────────────────────────────────

async function handleMessage(request, env, ctx) {
  const rawBody = await request.text();

  // Validar firma X-Hub-Signature-256
  const signature = request.headers.get("x-hub-signature-256") ?? "";
  const valid = await verifySignature(rawBody, signature, env.META_APP_SECRET);
  if (!valid) {
    console.error("Firma invalida — payload descartado");
    return new Response("Forbidden", { status: 403 });
  }

  // Responder a Meta inmediatamente y reenviar en background con ctx.waitUntil
  ctx.waitUntil(forwardToBackend(rawBody, env));
  return new Response("OK", { status: 200 });
}

// ── Validación de firma HMAC-SHA256 ──────────────────────────────────────────

async function verifySignature(body, signatureHeader, appSecret) {
  if (!appSecret || !signatureHeader) return false;

  const expected = signatureHeader.replace("sha256=", "");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(appSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const computed = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  return computed === expected;
}

// ── Forward al backend Python ─────────────────────────────────────────────────

async function forwardToBackend(rawBody, env) {
  if (!env.BACKEND_URL) {
    console.warn("BACKEND_URL no configurado — payload descartado");
    return;
  }
  try {
    const resp = await fetch(`${env.BACKEND_URL}/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Worker-Secret": env.BACKEND_SECRET ?? "",
      },
      body: rawBody,
    });
    if (!resp.ok) {
      console.error(`Backend respondio ${resp.status}`);
    }
  } catch (err) {
    console.error("Error al contactar backend:", err);
  }
}
