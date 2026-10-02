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

create table if not exists public.scanner_watchdog_state (
  service text primary key,
  alert_open boolean not null default false,
  last_alert_at timestamptz,
  last_recovered_at timestamptz,
  updated_at timestamptz not null default now()
);

alter table public.scanner_watchdog_state enable row level security;
