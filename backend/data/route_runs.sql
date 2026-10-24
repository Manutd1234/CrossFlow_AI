-- Durable, shared storage for route-solver runs.
-- Run once in the Supabase SQL editor used by the backend server key.
--
-- Without this table the API persists routes only in SQLite, which on Vercel
-- lives in per-invocation /tmp. A driver code issued by one worker is then
-- invisible to the next, so assigned-route retrieval fails even with a valid
-- token. This table makes a run readable from every instance and gives the
-- history view a single shared source.
-- Rerunnable upgrade: retains canonical routes and backfills only the account
-- attribution still present in an older table. Previously overwritten owners
-- cannot be reconstructed. Deploy this SQL before the matching backend code.
begin;

create table if not exists public.crossflow_route_runs (
  route_id text primary key,
  route_code text not null unique,
  route_kind text not null,
  request jsonb not null,
  response jsonb not null,
  -- Denormalized for the history list so it never has to parse every envelope.
  origin_name text,
  destination_name text,
  vehicle_type text,
  -- First-seen attribution retained for legacy compatibility. Account history
  -- lives separately below because many accounts can plan this same route.
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists crossflow_route_runs_created_at_idx
on public.crossflow_route_runs (created_at desc);

create index if not exists crossflow_route_runs_created_by_idx
on public.crossflow_route_runs (created_by, created_at desc);

create or replace function public.crossflow_touch_route_run()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  -- A route_id is content-addressed, so a repeat run is the same route. Keep
  -- the original created_at and let updated_at record that it was replanned.
  if tg_op = 'UPDATE' then
    new.created_at := old.created_at;
    new.created_by := old.created_by;
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists crossflow_route_runs_touch
on public.crossflow_route_runs;

create trigger crossflow_route_runs_touch
before insert or update on public.crossflow_route_runs
for each row execute function public.crossflow_touch_route_run();

alter table public.crossflow_route_runs enable row level security;

create table if not exists public.crossflow_route_run_accounts (
  route_id text not null references public.crossflow_route_runs(route_id) on delete cascade,
  account_key text not null,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (route_id, account_key),
  check (account_key = coalesce(created_by::text, ''))
);

create index if not exists crossflow_route_run_accounts_created_by_idx
on public.crossflow_route_run_accounts (created_by, created_at desc);

-- Preserve the remaining legacy attribution without changing existing route
-- rows or resetting any association already recorded by the new backend.
insert into public.crossflow_route_run_accounts (
  route_id, account_key, created_by, created_at, updated_at
)
select route_id, coalesce(created_by::text, ''), created_by, created_at, updated_at
from public.crossflow_route_runs
on conflict (route_id, account_key) do nothing;

alter table public.crossflow_route_run_accounts enable row level security;

-- Each account receives its own first-seen timestamp. Admins filter
-- account_rank=1 to list each public route once, matching local SQLite history.
-- Account-scoped reads filter created_by instead, preserving every association.
create or replace view public.crossflow_route_run_history
with (security_invoker = true)
as
select
  route.route_id, route.route_code, route.route_kind,
  route.origin_name, route.destination_name, route.vehicle_type,
  history.created_by, history.created_at, history.updated_at,
  row_number() over (
    partition by history.route_id
    order by history.created_at desc, history.account_key
  ) as account_rank
from public.crossflow_route_run_accounts as history
join public.crossflow_route_runs as route on route.route_id = history.route_id;

-- One RPC transaction records both the canonical public envelope and the
-- caller/guest association. A failed association cannot partially save a run.
create or replace function public.crossflow_save_route_run(p_run jsonb)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  caller_id uuid := (p_run ->> 'created_by')::uuid;
begin
  insert into public.crossflow_route_runs (
    route_id, route_code, route_kind, request, response,
    origin_name, destination_name, vehicle_type, created_by
  ) values (
    p_run ->> 'route_id', p_run ->> 'route_code', p_run ->> 'route_kind',
    p_run -> 'request', p_run -> 'response', p_run ->> 'origin_name',
    p_run ->> 'destination_name', p_run ->> 'vehicle_type', caller_id
  )
  on conflict (route_id) do update set
    response = excluded.response,
    origin_name = excluded.origin_name,
    destination_name = excluded.destination_name,
    vehicle_type = excluded.vehicle_type;

  insert into public.crossflow_route_run_accounts (route_id, account_key, created_by)
  values (p_run ->> 'route_id', coalesce(caller_id::text, ''), caller_id)
  on conflict (route_id, account_key) do update set updated_at = now();
end;
$$;

-- No anon/authenticated policy is intentionally created. The backend accesses
-- this table only with SUPABASE_SECRET_KEY (or the legacy service-role key),
-- both of which bypass RLS. Route history is served through the API, which
-- applies its own authorization, never by the browser querying this directly.
revoke all on table public.crossflow_route_runs from public, anon, authenticated;
revoke all on table public.crossflow_route_run_accounts from public, anon, authenticated;
revoke all on table public.crossflow_route_run_history from public, anon, authenticated;
grant select, insert, update on table public.crossflow_route_runs to service_role;
grant select, insert, update on table public.crossflow_route_run_accounts to service_role;
grant select on table public.crossflow_route_run_history to service_role;
revoke all on function public.crossflow_save_route_run(jsonb) from public, anon, authenticated;
grant execute on function public.crossflow_save_route_run(jsonb) to service_role;

commit;
