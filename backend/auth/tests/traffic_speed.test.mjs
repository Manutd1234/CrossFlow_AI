/** Verify stopped measurements and the legacy CHECK upgrade in PostgreSQL. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const schema = await readFile(new URL('../../data/routing_intelligence.sql', import.meta.url), 'utf8');
const migration = await readFile(new URL('../../data/migrations/003_allow_stopped_traffic_observations.sql', import.meta.url), 'utf8');
const tableAndUpgrade = schema.slice(0, schema.indexOf('create extension if not exists pgcrypto'));
const legacyTable = tableAndUpgrade.slice(0, tableAndUpgrade.indexOf('-- A stopped vehicle'))
  .replace('constraint crossflow_spatial_actual_speed_check\n    check (actual_speed_kph >= 0 and actual_speed_kph <= 250)',
    'check (actual_speed_kph > 0 and actual_speed_kph <= 250)')
  .replace('constraint crossflow_spatial_speed_ratio_check\n    check (actual_speed_kph / free_flow_speed_kph between 0 and 1.50)',
    'check (actual_speed_kph / free_flow_speed_kph between 0.05 and 1.50)');

let identity = 0;
async function insert(db, actual = 30, freeFlow = 60, source = 'probe_gps') {
  return db.query(`insert into public.crossflow_spatial_traffic_observations (
    observation_key, observed_at, corridor_id, latitude, longitude,
    actual_speed_kph, free_flow_speed_kph, source, provenance, confidence,
    observed, reviewed, road_class, local_timezone_offset_minutes, immutable_payload
  ) values ($1, '2026-10-23T00:00:00Z', 'corridor-1', 1.1, 104.0,
    $2, $3, $4, 'verified_gps', 0.95, true, false, 'primary', 420, '{}')
  returning observation_key, ingestion_revision, actual_speed_kph, free_flow_speed_kph`,
  [(++identity).toString(16).padStart(64, '0'), actual, freeFlow, source]);
}

test('fresh PostgreSQL schema accepts stopped/near-stopped traffic and retains physical bounds', async () => {
  const db = await PGlite.create();
  try {
    await db.exec(tableAndUpgrade);
    for (const actual of [0, 0.001, 2]) {
      assert.equal((await insert(db, actual)).rows[0].actual_speed_kph, actual);
    }
    for (const [actual, free] of [[-0.001, 60], [251, 200], [0, 0], [0, -1],
      [0, 251], ['NaN', 60], ['Infinity', 60], [0, 'NaN'], [0, 'Infinity'], [90.001, 60]]) {
      await assert.rejects(insert(db, actual, free), /check constraint|division by zero/);
    }
    assert.equal((await insert(db, 90, 60)).rows[0].actual_speed_kph, 90);
  } finally { await db.close(); }
});

test('speed-only upgrade preserves rows, identity and source safeguards and is rerunnable', async () => {
  const db = await PGlite.create();
  try {
    await db.exec(legacyTable);
    const prior = (await insert(db)).rows[0];
    await assert.rejects(insert(db, 0), /check constraint/);
    await assert.rejects(insert(db, 1), /check constraint/);
    for (let run = 0; run < 2; run += 1) {
      await db.exec(migration);
      assert.equal((await insert(db, 0)).rows[0].actual_speed_kph, 0);
      assert.equal((await insert(db, 1)).rows[0].actual_speed_kph, 1);
      await assert.rejects(insert(db, 30, 60, 'synthetic'), /check constraint/);
      await assert.rejects(insert(db, 91), /check constraint/);
    }
    assert.deepEqual((await db.query(`select observation_key, ingestion_revision,
      actual_speed_kph, free_flow_speed_kph from public.crossflow_spatial_traffic_observations
      where observation_key=$1`, [prior.observation_key])).rows[0], prior);
    // Full setup reruns use the same targeted upgrade and preserve measurements.
    await db.exec(tableAndUpgrade);
    assert.equal((await db.query('select count(*)::int as total from public.crossflow_spatial_traffic_observations')).rows[0].total, 5);
  } finally { await db.close(); }
});

test('a failed migration restores original constraints and all observations atomically', async () => {
  const db = await PGlite.create();
  try {
    await db.exec(legacyTable);
    const prior = (await insert(db)).rows[0];
    await assert.rejects(db.exec(migration.replace('commit;', 'select 1 / 0;\ncommit;')), /division by zero/);
    await db.exec('rollback');
    await assert.rejects(insert(db, 0), /check constraint/);
    assert.equal((await db.query('select observation_key from public.crossflow_spatial_traffic_observations')).rows[0].observation_key, prior.observation_key);
  } finally { await db.close(); }
});
