
const SERVICE = "v3-scanner";
const EXPECTED_REPOSITORY = "DelDeto/High-Volatility-Setup-Watch";
const EXPECTED_REF = "refs/heads/main";
const EXPECTED_AUDIENCE = "v34-supabase-watchdog";
const GITHUB_ISSUER = "https://token.actions.githubusercontent.com";
const GITHUB_JWKS = "https://token.actions.githubusercontent.com/.well-known/jwks";
const STALE_MS = 25 * 60 * 1000;
const REPEAT_ALERT_MS = 60 * 60 * 1000;

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function base64UrlBytes(input: string): Uint8Array {
  const normalized = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function decodePart(input: string): any {
  return JSON.parse(new TextDecoder().decode(base64UrlBytes(input)));
}

async function verifyGithubOidc(token: string) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("invalid GitHub OIDC token");

  const header = decodePart(parts[0]);
  const payload = decodePart(parts[1]);

  if (header.alg !== "RS256" || !header.kid) throw new Error("unexpected JWT header");

  const jwksRes = await fetch(GITHUB_JWKS);
  if (!jwksRes.ok) throw new Error("GitHub JWKS unavailable");
  const jwks = await jwksRes.json();
  const jwk = jwks.keys?.find((k: any) => k.kid === header.kid);
  if (!jwk) throw new Error("GitHub signing key not found");

  const key = await crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );

  const signed = new TextEncoder().encode(parts[0] + "." + parts[1]);
  const signature = base64UrlBytes(parts[2]);
  const valid = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5",
    key,
    signature,
    signed,
  );
  if (!valid) throw new Error("invalid GitHub OIDC signature");

  const now = Math.floor(Date.now() / 1000);
  const audience = Array.isArray(payload.aud) ? payload.aud : [payload.aud];

  if (payload.iss !== GITHUB_ISSUER) throw new Error("invalid issuer");
  if (!audience.includes(EXPECTED_AUDIENCE)) throw new Error("invalid audience");
  if (!payload.exp || payload.exp < now - 30) throw new Error("expired token");
  if (payload.nbf && payload.nbf > now + 30) throw new Error("token not active");
  if (payload.repository !== EXPECTED_REPOSITORY) throw new Error("invalid repository");
  if (payload.ref !== EXPECTED_REF) throw new Error("invalid ref");
  if (!["schedule", "workflow_dispatch"].includes(payload.event_name)) {
    throw new Error("invalid event");
  }

  const expectedWorkflow = EXPECTED_REPOSITORY + "/.github/workflows/v3-market-scan.yml@refs/heads/main";
  if (payload.workflow_ref !== expectedWorkflow) throw new Error("invalid workflow");

  return payload;
}

function adminKey(): string {
  const secretJson = Deno.env.get("SUPABASE_SECRET_KEYS");
  if (secretJson) {
    const parsed = JSON.parse(secretJson);
    if (parsed.default) return parsed.default;
  }
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (legacy) return legacy;
  throw new Error("missing Supabase admin key");
}

async function db(
  table: string,
  method: string,
  query = "",
  body?: unknown,
  prefer?: string,
) {
  const url = Deno.env.get("SUPABASE_URL");
  if (!url) throw new Error("missing SUPABASE_URL");

  const headers: Record<string, string> = {
    apikey: adminKey(),
    "content-type": "application/json",
  };
  if (prefer) headers.Prefer = prefer;

  const res = await fetch(
    url + "/rest/v1/" + table + (query ? "?" + query : ""),
    {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    },
  );

  if (!res.ok) {
    const text = await res.text();
    throw new Error("database request failed " + res.status + ": " + text.slice(0, 300));
  }

  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

async function selectOne(table: string) {
  const rows = await db(
    table,
    "GET",
    "service=eq." + encodeURIComponent(SERVICE) + "&select=*",
  );
  return Array.isArray(rows) ? rows[0] ?? null : null;
}

async function upsert(table: string, payload: unknown) {
  return await db(
    table,
    "POST",
    "on_conflict=service",
    payload,
    "resolution=merge-duplicates,return=representation",
  );
}

async function telegram(botToken: string, chatId: string, text: string) {
  const body = new URLSearchParams({ chat_id: chatId, text });
  const res = await fetch(
    "https://api.telegram.org/bot" + botToken + "/sendMessage",
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    },
  );
  if (!res.ok) throw new Error("Telegram HTTP " + res.status);
  const payload = await res.json();
  if (!payload.ok) throw new Error("Telegram API returned ok=false");
}

async function handleHeartbeat(req: Request) {
  const oidc = req.headers.get("x-github-oidc") ?? "";
  if (!oidc) return json({ ok: false, error: "missing GitHub OIDC" }, 401);

  let claims;
  try {
    claims = await verifyGithubOidc(oidc);
  } catch (error) {
    return json({ ok: false, error: String(error) }, 401);
  }

  const body = await req.json();
  const telegramBotToken = String(body.telegram_bot_token ?? "");
  const telegramChatId = String(body.telegram_chat_id ?? "");
  if (!telegramBotToken || !telegramChatId) {
    return json({ ok: false, error: "missing Telegram credentials" }, 400);
  }

  await upsert("scanner_heartbeat", {
    service: SERVICE,
    last_completed_slot: body.last_completed_slot ?? null,
    last_completed_at: body.last_completed_at ?? new Date().toISOString(),
    run_id: String(body.run_id ?? claims.run_id ?? ""),
    status: "healthy",
    pending_delivery: Number(body.pending_delivery ?? 0),
    updated_at: new Date().toISOString(),
  });

  await db(
    "scanner_watchdog_config",
    "PATCH",
    "service=eq." + encodeURIComponent(SERVICE),
    {
      telegram_bot_token: telegramBotToken,
      telegram_chat_id: telegramChatId,
      updated_at: new Date().toISOString(),
    },
    "return=minimal",
  );

  return json({
    ok: true,
    mode: "heartbeat",
    slot: body.last_completed_slot ?? null,
    pending_delivery: Number(body.pending_delivery ?? 0),
  });
}

async function handleCron(req: Request) {
  const config = await selectOne("scanner_watchdog_config");
  if (!config) return json({ ok: false, error: "watchdog config missing" }, 500);

  const cronToken = req.headers.get("x-watchdog-cron") ?? "";
  if (!cronToken || cronToken !== config.cron_token) {
    return json({ ok: false, error: "invalid cron token" }, 401);
  }

  const heartbeat = await selectOne("scanner_heartbeat");
  const state = await selectOne("scanner_watchdog_state");
  const now = Date.now();

  const completedAt = heartbeat?.last_completed_at
    ? new Date(heartbeat.last_completed_at).getTime()
    : 0;
  const ageMs = completedAt > 0 ? now - completedAt : Number.POSITIVE_INFINITY;
  const stale = !heartbeat || ageMs > STALE_MS;

  const alertOpen = Boolean(state?.alert_open);
  const lastAlertMs = state?.last_alert_at
    ? new Date(state.last_alert_at).getTime()
    : 0;
  const shouldRepeat = !lastAlertMs || now - lastAlertMs > REPEAT_ALERT_MS;

  const botToken = config.telegram_bot_token ?? "";
  const chatId = config.telegram_chat_id ?? "";
  const telegramConfigured = Boolean(botToken && chatId);

  if (stale && telegramConfigured && (!alertOpen || shouldRepeat)) {
    const ageMinutes = Number.isFinite(ageMs) ? Math.floor(ageMs / 60000) : -1;
    await telegram(
      botToken,
      chatId,
      [
        "🚨 V3.4 EXTERNAL WATCHDOG",
        "GitHub scanner heartbeat is stale.",
        ageMinutes >= 0
          ? "Last successful heartbeat: " + ageMinutes + " min ago"
          : "No successful heartbeat has been recorded.",
        "Pending Telegram items: " + String(heartbeat?.pending_delivery ?? "unknown"),
        "",
        "GitHub catch-up will replay recent missed slots when Actions returns.",
      ].join("\n"),
    );

    await upsert("scanner_watchdog_state", {
      service: SERVICE,
      alert_open: true,
      last_alert_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }

  if (!stale && telegramConfigured && alertOpen) {
    await telegram(
      botToken,
      chatId,
      [
        "✅ V3.4 EXTERNAL WATCHDOG RECOVERED",
        "Supabase sees a fresh scanner heartbeat again.",
        "GitHub monitoring is back online.",
      ].join("\n"),
    );

    await upsert("scanner_watchdog_state", {
      service: SERVICE,
      alert_open: false,
      last_recovered_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }

  return json({
    ok: true,
    mode: "cron",
    stale,
    heartbeat_age_seconds: Number.isFinite(ageMs)
      ? Math.floor(ageMs / 1000)
      : null,
    pending_delivery: heartbeat?.pending_delivery ?? null,
    telegram_configured: telegramConfigured,
    alert_open: stale ? true : false,
  });
}

Deno.serve(async (req: Request) => {
  try {
    if (req.method !== "POST") return json({ ok: false, error: "method not allowed" }, 405);

    if (req.headers.has("x-github-oidc")) {
      return await handleHeartbeat(req);
    }

    if (req.headers.has("x-watchdog-cron")) {
      return await handleCron(req);
    }

    return json({ ok: false, error: "unauthorized" }, 401);
  } catch (error) {
    console.error(error);
    return json({ ok: false, error: String(error) }, 500);
  }
});
