-- Preserve existing observations, policies, ownership and grants.
-- Run against an existing routing_intelligence.sql installation.
-- The model/router still floors usable speed ratios at 0.05.
begin;

-- A stopped vehicle is a valid measurement. Replace only checks involving
-- actual/free-flow speed, including the original unnamed constraints. The
-- DO statement is atomic, preserves every row and is safe to rerun.
do $$
declare
  actual_column smallint;
  free_flow_column smallint;
  constraint_name text;
begin
  select attnum into actual_column from pg_attribute
    where attrelid = 'public.crossflow_spatial_traffic_observations'::regclass
      and attname = 'actual_speed_kph';
  select attnum into free_flow_column from pg_attribute
    where attrelid = 'public.crossflow_spatial_traffic_observations'::regclass
      and attname = 'free_flow_speed_kph';
  for constraint_name in
    select conname from pg_constraint
    where conrelid = 'public.crossflow_spatial_traffic_observations'::regclass
      and contype = 'c'
      and conkey @> array[actual_column]::smallint[]
      and conkey <@ array[actual_column, free_flow_column]::smallint[]
  loop
    execute format('alter table public.crossflow_spatial_traffic_observations drop constraint %I', constraint_name);
  end loop;
  alter table public.crossflow_spatial_traffic_observations
    add constraint crossflow_spatial_actual_speed_check
      check (actual_speed_kph >= 0 and actual_speed_kph <= 250),
    add constraint crossflow_spatial_speed_ratio_check
      check (actual_speed_kph / free_flow_speed_kph between 0 and 1.50);
end;
$$;

commit;
