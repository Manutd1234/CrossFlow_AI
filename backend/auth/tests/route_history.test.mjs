/** Exercise the shared route/history SQL against real PostgreSQL semantics. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const schema = await readFile(new URL('../../data/route_runs.sql', import.meta.url), 'utf8');
const ACCOUNT_A = '11111111-1111-4111-8111-111111111111';
const ACCOUNT_B = '22222222-2222-4222-8222-222222222222';
const ROUTE = 'a'.repeat(64);

async function fixture(beforeSchema) {
  const db = await PGlite.create();
  await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
  if (beforeSchema) await beforeSchema(db);
  await db.exec(schema);
  return db;
}

function run(createdBy, routeId = ROUTE, routeCode = 'ABCDEFG') {
  return {
    route_id: routeId, route_code: routeCode, route_kind: 'optimize-route',
    request: { route: 'same public journey' }, response: { route_id: routeId, route_code: routeCode },
    origin_name: 'Origin', destination_name: 'Destination', vehicle_type: 'COMMUTER',
    created_by: createdBy,
  };
}

async function save(db, createdBy, routeId, routeCode) {
  await db.exec('set role service_role');
  try {
    await db.query('select public.crossflow_save_route_run($1::jsonb)', [
      JSON.stringify(run(createdBy, routeId, routeCode)),
    ]);
  } finally { await db.exec('reset role'); }
}

async function own(db, account) {
  return (await db.query(`select route_id, route_code, created_by, created_at, updated_at
    from public.crossflow_route_run_history where created_by = $1`, [account])).rows;
}

test('two accounts and repeated guest planning retain independent histories and one public route', async () => {
  const db = await fixture();
  try {
    await save(db, ACCOUNT_A);
    const firstA = await own(db, ACCOUNT_A);
    await save(db, ACCOUNT_B);
    const firstB = await own(db, ACCOUNT_B);
    await save(db, null);
    await save(db, ACCOUNT_A);
    await save(db, null);
    for (const [account, first] of [[ACCOUNT_A, firstA], [ACCOUNT_B, firstB]]) {
      const rows = await own(db, account);
      assert.equal(rows.length, 1);
      assert.equal(rows[0].route_id, ROUTE);
      assert.equal(rows[0].route_code, 'ABCDEFG');
      assert.equal(rows[0].created_by, account);
      assert.deepEqual(rows[0].created_at, first[0].created_at);
    }
    assert.equal((await db.query('select * from public.crossflow_route_run_accounts')).rows.length, 3);
    const canonical = (await db.query('select created_by, response from public.crossflow_route_runs')).rows;
    assert.deepEqual(canonical, [{ created_by: ACCOUNT_A, response: run(ACCOUNT_A).response }]);
    const admin = (await db.query('select route_code from public.crossflow_route_run_history where account_rank = 1')).rows;
    assert.deepEqual(admin, [{ route_code: 'ABCDEFG' }]);
    const publicLookup = (await db.query(`select response from public.crossflow_route_runs where route_code = 'ABCDEFG'`)).rows;
    assert.deepEqual(publicLookup, [{ response: run(ACCOUNT_A).response }]);
    // A route first created by a guest also becomes visible to each later
    // account without clearing the original guest association.
    await save(db, null, 'b'.repeat(64), 'BCDEFGH');
    await save(db, ACCOUNT_A, 'b'.repeat(64), 'BCDEFGH');
    await save(db, ACCOUNT_B, 'b'.repeat(64), 'BCDEFGH');
    assert.equal((await own(db, ACCOUNT_A)).length, 2);
    assert.equal((await own(db, ACCOUNT_B)).length, 2);
  } finally { await db.close(); }
});

test('legacy attribution backfills once without changing canonical data or resetting account timestamps', async () => {
  const db = await fixture(async database => {
    await database.exec(`create table public.crossflow_route_runs (
      route_id text primary key, route_code text not null unique, route_kind text not null,
      request jsonb not null, response jsonb not null, origin_name text, destination_name text,
      vehicle_type text, created_by uuid, created_at timestamptz not null default now(),
      updated_at timestamptz not null default now()
    )`);
    await database.query(`insert into public.crossflow_route_runs
      (route_id, route_code, route_kind, request, response, created_by, created_at, updated_at)
      values ($1, 'ABCDEFG', 'optimize-route', '{}', '{}', $2, '2026-01-01Z', '2026-01-02Z')`, [ROUTE, ACCOUNT_A]);
  });
  try {
    const before = (await db.query('select * from public.crossflow_route_runs')).rows;
    const original = await own(db, ACCOUNT_A);
    assert.equal(original.length, 1);
    assert.equal(original[0].created_at.toISOString(), '2026-01-01T00:00:00.000Z');
    await save(db, ACCOUNT_B);
    const historyBeforeRerun = (await db.query('select * from public.crossflow_route_run_accounts order by account_key')).rows;
    await db.exec(schema);
    assert.deepEqual((await db.query('select * from public.crossflow_route_run_accounts order by account_key')).rows, historyBeforeRerun);
    assert.equal((await db.query('select created_by from public.crossflow_route_runs')).rows[0].created_by, before[0].created_by);
    assert.deepEqual((await own(db, ACCOUNT_A))[0].created_at, original[0].created_at);
  } finally { await db.close(); }
});

test('shared history tables, view and save RPC remain inaccessible to browser roles', async () => {
  const db = await fixture();
  try {
    await save(db, ACCOUNT_A);
    for (const role of ['anon', 'authenticated']) {
      await db.exec(`set role ${role}`);
      try {
        for (const relation of ['crossflow_route_runs', 'crossflow_route_run_accounts', 'crossflow_route_run_history']) {
          await assert.rejects(db.query(`select * from public.${relation}`), error => error.code === '42501');
        }
        await assert.rejects(db.query('select public.crossflow_save_route_run($1::jsonb)', [JSON.stringify(run(ACCOUNT_B))]), error => error.code === '42501');
      } finally { await db.exec('reset role'); }
    }
    await db.exec('set role service_role');
    try {
      assert.equal((await db.query('select * from public.crossflow_route_run_history')).rows.length, 1);
    } finally { await db.exec('reset role'); }
  } finally { await db.close(); }
});

test('an association failure rolls back its canonical route write', async () => {
  const db = await fixture();
  try {
    await db.exec(`create function public.reject_test_history() returns trigger language plpgsql as $$
      begin raise exception 'association unavailable' using errcode = 'check_violation'; end; $$;
      create trigger reject_test_history before insert on public.crossflow_route_run_accounts
      for each row execute function public.reject_test_history();`);
    await assert.rejects(save(db, ACCOUNT_A), error => error.code === '23514');
    assert.equal((await db.query('select * from public.crossflow_route_runs')).rows.length, 0);
    assert.equal((await db.query('select * from public.crossflow_route_run_accounts')).rows.length, 0);
  } finally { await db.close(); }
});
