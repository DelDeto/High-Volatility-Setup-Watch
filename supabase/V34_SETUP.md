# V3.4 Supabase External Watchdog

This folder contains the zero-cost external watchdog layer for the market scanner.

## Architecture

GitHub production scan writes a successful heartbeat to `scanner_heartbeat`.
Supabase Cron runs the `scanner-watchdog` Edge Function independently of GitHub.
If the heartbeat is older than 25 minutes, Supabase sends a Telegram outage alert.
When GitHub becomes available again, V3.4 catch-up replays up to 8 missed 15-minute slots and the next successful heartbeat closes the external alert.

## One-time activation

1. Apply `supabase/migrations/202610020001_v34_watchdog.sql`.
2. Deploy `supabase/functions/scanner-watchdog/index.ts`.
3. Add Edge Function secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. In Supabase Vault, create:
   - `project_url` = your Supabase project URL
   - `publishable_key` = your project publishable key
5. Schedule the Edge Function every 10 minutes using Supabase Cron:

```sql
select cron.schedule(
  'v34-scanner-watchdog',
  '*/10 * * * *',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_url')
           || '/functions/v1/scanner-watchdog',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'apikey', (select decrypted_secret from vault.decrypted_secrets where name = 'publishable_key')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 5000
  );
  $$
);
```

6. Add GitHub Actions repository secrets:
   - `SUPABASE_PROJECT_URL`
   - `SUPABASE_SECRET_KEY`

The secret key must remain server-side. Do not commit it to the repository.

Supabase Cron + Edge Functions are independent of GitHub Actions scheduling, so they can detect a GitHub-side outage even when both the primary scanner cron and GitHub watchdog cron are silent.
