import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";
import { createRemoteJWKSet, jwtVerify } from "npm:jose@6.1.0";

const SERVICE = "v3-scanner";
const EXPECTED_REPOSITORY = "DelDeto/High-Volatility-Setup-Watch";
const EXPECTED_REPOSITORY_ID = "1400391412";
const EXPECTED_REF = "refs/heads/main";
const EXPECTED_AUDIENCE = "supabase-v34-heartbeat";
const ISSUER = "https://token.actions.githubusercontent.com";
const JWKS = createRemoteJWKSet(new URL(ISSUER + "/.well-known/jwks"));

function adminClient() {
  const url = Deno.env.get("SUPABASE_URL")!;
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const modernRaw = Deno.env.get("SUPABASE_SECRET_KEYS") || "{}";
  const modern = JSON.parse(modernRaw)["default"];
  const key = modern || legacy;
  if (!url || !key) throw new Error("missing Supabase admin credentials");
  return createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false } });
}

async function verifyGitHubOidc(req: Request) {
  const auth = req.headers.get("authorization") || "";
  if (!auth.startsWith("Bearer ")) throw new Error("missing bearer token");
  const token = auth.slice(7);

  const { payload } = await jwtVerify(token, JWKS, {
    issuer: ISSUER,
    audience: EXPECTED_AUDIENCE,
  });

  if (payload.repository !== EXPECTED_REPOSITORY) throw new Error("unexpected repository");
  if (String(payload.repository_id || "") !== EXPECTED_REPOSITORY_ID) throw new Error("unexpected repository id");
  if (payload.ref !== EXPECTED_REF) throw new Error("unexpected ref");
  if (!["schedule", "workflow_dispatch", "push"].includes(String(payload.event_name || ""))) {
    throw new Error("unexpected event");
  }
  return payload;
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  try {
    const claims = await verifyGitHubOidc(req);
    const body = await req.json();
    const supabase = adminClient();
    const now = new Date().toISOString();

    const heartbeat = {
      service: SERVICE,
      last_completed_slot: body.last_completed_slot ?? null,
      last_completed_at: body.last_completed_at ?? now,
      run_id: body.run_id ? String(body.run_id) : String(claims.run_id || ""),
      status: body.status || "healthy",
      pending_delivery: Number(body.pending_delivery || 0),
      updated_at: now,
    };

    const { error: heartbeatError } = await supabase
      .from("scanner_heartbeat")
      .upsert(heartbeat, { onConflict: "service" });
    if (heartbeatError) throw heartbeatError;

    if (body.telegram_bot_token && body.telegram_chat_id) {
      const { error: configError } = await supabase
        .from("scanner_watchdog_config")
        .upsert({
          service: SERVICE,
          telegram_bot_token: String(body.telegram_bot_token),
          telegram_chat_id: String(body.telegram_chat_id),
          updated_at: now,
        }, { onConflict: "service" });
      if (configError) throw configError;
    }

    return Response.json({
      ok: true,
      service: SERVICE,
      last_completed_slot: heartbeat.last_completed_slot,
      pending_delivery: heartbeat.pending_delivery,
    });
  } catch (error) {
    console.error("heartbeat rejected", error instanceof Error ? error.message : String(error));
    return Response.json({ ok: false, error: "unauthorized or invalid heartbeat" }, { status: 401 });
  }
});
