import { withSupabase } from "npm:@supabase/server@1";

const SERVICE = "v3-scanner";
const STALE_MS = 25 * 60 * 1000;
const REPEAT_ALERT_MS = 60 * 60 * 1000;

async function telegram(text: string) {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";
  const chatId = Deno.env.get("TELEGRAM_CHAT_ID") ?? "";
  if (!token || !chatId) throw new Error("missing Telegram secrets");

  const body = new URLSearchParams({ chat_id: chatId, text });
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error(`Telegram HTTP ${res.status}`);
  const json = await res.json();
  if (!json.ok) throw new Error("Telegram API returned ok=false");
}

export default {
  fetch: withSupabase({ auth: "secret" }, async (_req, ctx) => {
    const supabase = ctx.supabaseAdmin;
    const now = Date.now();

    const { data: heartbeat, error: heartbeatError } = await supabase
      .from("scanner_heartbeat")
      .select("*")
      .eq("service", SERVICE)
      .maybeSingle();

    if (heartbeatError) throw heartbeatError;

    const { data: watchdogState, error: stateError } = await supabase
      .from("scanner_watchdog_state")
      .select("*")
      .eq("service", SERVICE)
      .maybeSingle();

    if (stateError) throw stateError;

    const completedAt = heartbeat?.last_completed_at
      ? new Date(heartbeat.last_completed_at).getTime()
      : 0;
    const ageMs = completedAt > 0 ? now - completedAt : Number.POSITIVE_INFINITY;
    const stale = !heartbeat || ageMs > STALE_MS;
    const alertOpen = Boolean(watchdogState?.alert_open);
    const lastAlert = watchdogState?.last_alert_at
      ? new Date(watchdogState.last_alert_at).getTime()
      : 0;
    const shouldRepeat = !lastAlert || now - lastAlert > REPEAT_ALERT_MS;

    if (stale && (!alertOpen || shouldRepeat)) {
      const ageMinutes = Number.isFinite(ageMs) ? Math.floor(ageMs / 60000) : -1;
      await telegram([
        "🚨 V3.4 EXTERNAL WATCHDOG",
        "GitHub scanner heartbeat is stale.",
        ageMinutes >= 0
          ? `Last successful heartbeat: ${ageMinutes} min ago`
          : "No successful heartbeat has been recorded.",
        `Pending Telegram items: ${heartbeat?.pending_delivery ?? "unknown"}`,
        "",
        "GitHub catch-up will replay recent missed slots when Actions returns.",
      ].join("\n"));

      const { error } = await supabase.from("scanner_watchdog_state").upsert({
        service: SERVICE,
        alert_open: true,
        last_alert_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
      if (error) throw error;
    }

    if (!stale && alertOpen) {
      await telegram([
        "✅ V3.4 EXTERNAL WATCHDOG RECOVERED",
        "Supabase sees a fresh scanner heartbeat again.",
        "GitHub monitoring is back online.",
      ].join("\n"));

      const { error } = await supabase.from("scanner_watchdog_state").upsert({
        service: SERVICE,
        alert_open: false,
        last_recovered_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
      if (error) throw error;
    }

    return Response.json({
      ok: true,
      stale,
      heartbeat_age_seconds: Number.isFinite(ageMs)
        ? Math.floor(ageMs / 1000)
        : null,
      pending_delivery: heartbeat?.pending_delivery ?? null,
      alert_open: stale,
    });
  }),
};
