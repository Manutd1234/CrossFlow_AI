-- Upgrade an existing CrossFlow auth schema v1 to v2. Safe to rerun.
-- Preconditions: schema v1 and its three functions already exist.
-- Run this complete file as the existing function owner (Supabase SQL Editor).
-- No existing profiles, roles, policies, triggers, grants, or owners change.
-- CREATE OR REPLACE preserves the existing functions' ownership and ACLs.

begin;

-- SECURITY DEFINER makes current_user the function owner, which bypasses the
-- existing role guard. Keep its tested body and evaluate it as the caller.
alter function public.crossflow_guard_profile_role() security invoker;

-- Preserve the existing signup trigger and driver-only role assignment while
-- recognizing Google provider display names for future signups.
create or replace function public.crossflow_handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.crossflow_profiles (id, role, display_name)
  values (
    new.id,
    'driver',
    left(
      coalesce(
        nullif(trim(new.raw_user_meta_data ->> 'display_name'), ''),
        -- Supabase's Google provider supplies full_name/name. These fields
        -- affect presentation only; the account role always stays driver.
        nullif(trim(new.raw_user_meta_data ->> 'full_name'), ''),
        nullif(trim(new.raw_user_meta_data ->> 'name'), ''),
        nullif(split_part(coalesce(new.email, ''), '@', 1), ''),
        'CrossFlow user'
      ),
      120
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create or replace function public.crossflow_auth_health()
returns table(
  schema_version integer,
  rls_enabled boolean,
  authenticated_can_read boolean,
  authenticated_cannot_insert boolean
)
language sql
security invoker
set search_path = ''
stable
as $$
  select
    2::integer,
    (
      select relrowsecurity
      from pg_catalog.pg_class
      where oid = 'public.crossflow_profiles'::regclass
    ),
    pg_catalog.has_table_privilege(
      'authenticated', 'public.crossflow_profiles', 'SELECT'
    ),
    not pg_catalog.has_table_privilege(
      'authenticated', 'public.crossflow_profiles', 'INSERT'
    )
$$;

commit;
