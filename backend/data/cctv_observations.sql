-- Service-only latest numeric CCTV evidence for Vercel/multi-instance APIs.
-- Apply once to the same Supabase project as the server-only backend key.
-- There are no stream URLs, image bytes, detections or browser grants here.
create table if not exists public.crossflow_cctv_latest (
  camera_id text primary key check (camera_id ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  captured_at timestamptz not null,
  evidence_sha256 text not null check (evidence_sha256 ~ '^[a-f0-9]{64}$'),
  evidence_json jsonb not null check (jsonb_typeof(evidence_json) = 'object' and octet_length(evidence_json::text) <= 32768),
  stored_at timestamptz not null
);
alter table public.crossflow_cctv_latest enable row level security;
revoke all on public.crossflow_cctv_latest from public, anon, authenticated;
grant select, insert, update on public.crossflow_cctv_latest to service_role;

create or replace function public.crossflow_ingest_cctv_observation(
  p_camera_id text, p_captured_at timestamptz, p_evidence_sha256 text,
  p_evidence jsonb, p_stored_at timestamptz
) returns jsonb language plpgsql security invoker set search_path = public as $$
declare previous public.crossflow_cctv_latest%rowtype;
begin
  -- Bounded latest-only records and deterministic serialization across workers.
  perform pg_advisory_xact_lock(702416241);
  select * into previous from public.crossflow_cctv_latest where camera_id = p_camera_id for update;
  if found and p_captured_at <= previous.captured_at then
    if p_captured_at = previous.captured_at and p_evidence_sha256 = previous.evidence_sha256 then
      return jsonb_build_object('status', 'accepted', 'idempotent', true,
        'camera_id', p_camera_id, 'observed_at', p_evidence->>'captured_at');
    end if;
    return jsonb_build_object('status', 'conflict');
  elsif not found and (select count(*) from public.crossflow_cctv_latest) >= 128 then
    return jsonb_build_object('status', 'capacity');
  end if;
  if p_evidence->>'camera_id' is distinct from p_camera_id
    or (p_evidence->>'captured_at')::timestamptz is distinct from p_captured_at
    or p_evidence->>'images_retained' is distinct from 'false'
    or p_evidence ?| array['image_base64', 'stream_url', 'source_url', 'detections'] then
    raise exception 'Invalid numeric CCTV evidence';
  end if;
  insert into public.crossflow_cctv_latest(camera_id, captured_at, evidence_sha256, evidence_json, stored_at)
    values (p_camera_id, p_captured_at, p_evidence_sha256, p_evidence, p_stored_at)
    on conflict(camera_id) do update set captured_at = excluded.captured_at,
      evidence_sha256 = excluded.evidence_sha256, evidence_json = excluded.evidence_json,
      stored_at = excluded.stored_at;
  return jsonb_build_object('status', 'accepted', 'idempotent', false,
    'camera_id', p_camera_id, 'observed_at', p_evidence->>'captured_at');
end;
$$;
revoke all on function public.crossflow_ingest_cctv_observation(text, timestamptz, text, jsonb, timestamptz) from public, anon, authenticated;
grant execute on function public.crossflow_ingest_cctv_observation(text, timestamptz, text, jsonb, timestamptz) to service_role;
