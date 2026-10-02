create extension if not exists pg_cron;
create extension if not exists pg_net;

create table if not exists public.scanner_heartbeat (
  service text primary key,
  last_completed_slot timestamptz,
  last_completed_at timestamptz,
  run_id text,
  status text not null default 'unknown',
  pending_delivery integer not null default 0,
  updated_at timestamptz not null default now()
);

alter table public.scanner_heartbeat enable row level security;
revoke all on public.scanner_heartbeat from anon, authenticated;
grant select, insert, update on public.scanner_heartbeat to service_role;

create table if not exists public.scanner_watchdog_state (
  service text primary key,
  alert_open boolean not null default false,
  last_alert_at timestamptz,
  last_recovered_at timestamptz,
  updated_at timestamptz not null default now()
);

alter table public.scanner_watchdog_state enable row level security;
revoke all on public.scanner_watchdog_state from anon, authenticated;
grant select, insert, update on public.scanner_watchdog_state to service_role;

create table if not exists public.scanner_watchdog_config (
  service text primary key,
  telegram_bot_token text,
  telegram_chat_id text,
  cron_token text not null,
  updated_at timestamptz not null default now()
);

alter table public.scanner_watchdog_config enable row level security;
revoke all on public.scanner_watchdog_config from anon, authenticated;
grant select, insert, update on public.scanner_watchdog_config to service_role;

insert into public.scanner_watchdog_config(service, cron_token)
values ('v3-scanner', encode(gen_random_bytes(48), 'hex'))
on conflict (service) do nothing;
