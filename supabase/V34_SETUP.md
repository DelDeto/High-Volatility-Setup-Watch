# V3.4 Supabase External Watchdog

This zero-cost layer is independent of GitHub Actions scheduling.

## Production setup

- Project: `v34-scanner-watchdog`
- Region: `ap-southeast-1` (Singapore)
- Edge Function: `scanner-watchdog`
- Cron: every 10 minutes
- Heartbeat source: GitHub Actions OIDC
- Stale threshold: 25 minutes
- Recovery: GitHub V3.4 catch-up + Telegram delivery retry

No additional GitHub secret is required. The production workflow requests a GitHub OIDC token with audience `v34-supabase-watchdog`. The Edge Function verifies the OIDC signature, repository, branch, event type and workflow path before accepting a heartbeat.

The heartbeat request securely passes the existing Telegram bot credentials from GitHub Actions to the RLS-protected watchdog config row. Supabase Cron then uses an internal random token stored only in Postgres to call the watchdog function every 10 minutes.

If the GitHub heartbeat becomes stale, Supabase sends a Telegram outage alert. When a fresh heartbeat returns, Supabase sends a recovered notification.

The schema is defined in `supabase/migrations/202610020001_v34_watchdog.sql`. The function runs with `verify_jwt=false` because GitHub OIDC and the private cron token are verified inside the function itself.
