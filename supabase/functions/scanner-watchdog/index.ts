import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";

const SERVICE = "v3-scanner";
const STALE_MS = 25 * 60 * 1000;
const REPEAT_ALERT_MS = 60 * 60 * 1000;

function adminClient() {
  const url = Deno.env.get("SUPABASE_URL")!;
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const modernRaw = Deno.env.get("SUPABASE_SECRET_KEYS") || "{}";
  const modern = JSON.parse(modernRaw)["default"];
  const key = modern || legacy;
  if (!url || !key) throw new Error("missing Supabase admin credentials");
  return createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false } });
}

async function telegram(token: string, chatId: string, text: string) {
  const body = new URLSearchParams({ chat_id: chatId, text });
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.ok) throw new Error(`Telegram send failed: HTTP ${res.status}`);
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  try {
    const supabase = adminClient();
    const headerToken = req.headers.get("x-watchdog-token") || "";

    const { data: config, error: configError } = await supabase
      .from("scanner_watchdog_config")
      .select("telegram_bot_token,telegram_chat_id,cron_token")
      .eq("service", SERVICE)
      .maybeSingle();
    if (configError) throw configError;

    if (!config?.cron_token || headerToken !== config.cron_token) {
      return Response.json({ ok: false, error: "unauthorized" }, { status: 401 });
    }

    const { data: heartbeat, error: heartbeatError } = await supabase
      .from("scanner_heartbeat")
      .select("*")
      .eq("service", SERVICE)
      .maybeSingle();
    if (heartbeatError) throw heartbeatError;

    const { data: state, error: stateError } = await supabase
      .from("scanner_watchdog_state")
      .select("*")
      .eq("service", SERVICE)
      .maybeSingle();
    if (stateError) throw stateError;

    const now = Date.now();
    const completedAt = heartbeat?.last_completed_at
      ? new Date(heartbeat.last_completed_at).getTime()
      : 0;
    const ageMs = completedAt > 0 ? now - completedAt : Number.POSITIVE_INFINITY;
    const stale = !heartbeat || ageMs > STALE_MS;

    const alertOpen = Boolean(state?.alert_open);
    const lastAlert = state?.last_alert_at ? new Date(state.last_alert_at).getTime() : 0;
    const shouldRepeat = !lastAlert || now - lastAlert > REPEAT_ALERT_MS;

    const botToken = config?.telegram_bot_token || "";
    const chatId = config?.telegram_chat_id || "";
    const canNotify = Boolean(botToken && chatId);

    if (stale && (!alertOpen || shouldRepeat)) {
      const ageMinutes = Number.isFinite(ageMs) ? Math.floor(ageMs / 60000) : -1;
      if (canNotify) {
        await telegram(botToken, chatId, [
          "🚨 V3.4 EXTERNAL WATCHDOG",
          "GitHub scanner heartbeat is stale.",
          ageMinutes >= 0
            ? `Last successful heartbeat: ${ageMinutes} min ago`
            : "No successful heartbeat has been recorded.",
          `Pending Telegram items: ${heartbeat?.pending_delivery ?? "unknown"}`,
          "",
          "GitHub catch-up will replay recent missed slots when Actions returns.",
        ].join("\n"));
      }

      const { error } = await supabase.from("scanner_watchdog_state").upsert({
        service: SERVICE,
        alert_open: true,
        last_alert_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }, { onConflict: "service" });
      if (error) throw error;
    }

    if (!stale && alertOpen) {
      if (canNotify) {
        await telegram(botToken, chatId, [
          "✅ V3.4 EXTERNAL WATCHDOG RECOVERED",
          "Supabase sees a fresh scanner heartbeat again.",
          "GitHub monitoring is back online.",
        ].join("\n"));
      }

      const { error } = await supabase.from("scanner_watchdog_state").upsert({
        service: SERVICE,
        alert_open: false,
        last_recovered_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }, { onConflict: "service" });
      if (error) throw error;
    }

    return Response.json({
      ok: true,
      stale,
      heartbeat_age_seconds: Number.isFinite(ageMs) ? Math.floor(ageMs / 1000) : null,
      pending_delivery: heartbeat?.pending_delivery ?? null,
      alert_open: stale,
      telegram_configured: canNotify,
    });
  } catch (error) {
    console.error("watchdog error", error instanceof Error ? error.message : String(error));
    return Response.json({ ok: false, error: "watchdog execution failed" }, { status: 500 });
  }
});
