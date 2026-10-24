/** Test the minimal hosted v1 upgrade without contacting Supabase. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const schema = await readFile(new URL('../schema.sql', import.meta.url), 'utf8');
const migration = await readFile(new URL('../migrations/002_harden_auth_role_guard.sql', import.meta.url), 'utf8');
const DRIVER = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const ADMIN = '33333333-3333-4333-8333-333333333333';

function definition(name) {
  const start = schema.indexOf(`create or replace function public.${name}()`);
  assert.notEqual(start, -1);
  return schema.slice(start, schema.indexOf('$$;', start) + 3);
}

async function fixture() {
  const db = await PGlite.create();
  await db.exec(`
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create schema auth;
    create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb default '{}');
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
    $$;
    grant usage on schema auth to authenticated, service_role;
  `);
  await db.exec(schema);
  // Reconstruct the confirmed hosted-v1 difference. Tables, grants, trigger
  // bindings and policies are otherwise the same deployed auth boundary.
  await db.exec('alter function public.crossflow_guard_profile_role() security definer');
  const oldSignup = definition('crossflow_handle_new_user')
    .replace(/\s*-- Supabase's Google provider supplies full_name\/name\. These fields\n\s*-- affect presentation only; the account role always stays driver\./, '')
    .replace(/\s*nullif\(trim\(new\.raw_user_meta_data ->> 'full_name'\), ''\),/, '')
    .replace(/\s*nullif\(trim\(new\.raw_user_meta_data ->> 'name'\), ''\),/, '');
  await db.exec(oldSignup);
  await db.exec(definition('crossflow_auth_health').replace('2::integer', '1::integer'));
  await db.query(`insert into auth.users (id, email, raw_user_meta_data) values
    ($1, 'driver@example.test', '{"display_name":"Existing Driver"}'),
    ($2, 'other@example.test', '{"display_name":"Other Existing Driver"}'),
    ($3, 'admin@example.test', '{"display_name":"Existing Admin"}')`, [DRIVER, OTHER, ADMIN]);
  await db.query(`update public.crossflow_profiles set role = 'admin' where id = $1`, [ADMIN]);
  return db;
}

async function snapshot(db) {
  return {
    profiles: (await db.query('select * from public.crossflow_profiles order by id')).rows,
    table: (await db.query(`select relowner, relacl::text, relrowsecurity
      from pg_class where oid = 'public.crossflow_profiles'::regclass`)).rows,
    functions: (await db.query(`select proname, proowner, proacl::text from pg_proc
      where pronamespace = 'public'::regnamespace and proname like 'crossflow_%' order by proname`)).rows,
    policies: (await db.query(`select polname, polcmd, polroles::text,
      pg_get_expr(polqual, polrelid) as using_clause,
      pg_get_expr(polwithcheck, polrelid) as check_clause
      from pg_policy where polrelid = 'public.crossflow_profiles'::regclass order by polname`)).rows,
    triggers: (await db.query(`select tgname, pg_get_triggerdef(oid) as definition from pg_trigger
      where not tgisinternal and tgrelid in ('public.crossflow_profiles'::regclass, 'auth.users'::regclass)
      order by tgname`)).rows,
  };
}

async function changedFunctions(db) {
  return (await db.query(`select proname, prosecdef, prosrc from pg_proc
    where pronamespace = 'public'::regnamespace and proname in
    ('crossflow_guard_profile_role', 'crossflow_handle_new_user', 'crossflow_auth_health')
    order by proname`)).rows;
}

async function asUser(db, id, action) {
  await db.query(`select set_config('request.jwt.claim.sub', $1, false)`, [id]);
  await db.exec('set role authenticated');
  try { return await action(); } finally {
    await db.exec('reset role');
    await db.exec('reset "request.jwt.claim.sub"');
  }
}

test('v1 upgrade is rerunnable and preserves all existing rows, grants, owners, policies and triggers', async () => {
  const db = await fixture();
  try {
    const before = await snapshot(db);
    assert.equal(before.profiles.length, 3);
    await db.exec(migration);
    assert.deepEqual(await snapshot(db), before);
    const after = await changedFunctions(db);
    assert.equal(after.find(row => row.proname === 'crossflow_guard_profile_role').prosecdef, false);
    assert.equal((await db.query('select schema_version from public.crossflow_auth_health()')).rows[0].schema_version, 2);
    // The replacement bodies are exactly the current schema's tested source.
    for (const name of ['crossflow_handle_new_user', 'crossflow_auth_health']) {
      const expectedBody = definition(name).split('as $$')[1].split('$$;')[0];
      assert.equal(after.find(row => row.proname === name).prosrc, expectedBody);
    }
    await db.exec(migration);
    assert.deepEqual(await snapshot(db), before);
    assert.deepEqual(await changedFunctions(db), after);
  } finally { await db.close(); }
});

test('migrated guard denies driver self-promotion and preserves administrator updates', async () => {
  const db = await fixture();
  try {
    await db.exec(migration);
    await assert.rejects(asUser(db, DRIVER, () => db.query(
      `update public.crossflow_profiles set role = 'admin' where id = $1`, [DRIVER],
    )), error => error.code === '42501');
    const updated = await asUser(db, ADMIN, () => db.query(
      `update public.crossflow_profiles set role = 'admin' where id = $1 returning role`, [OTHER],
    ));
    assert.deepEqual(updated.rows, [{ role: 'admin' }]);
  } finally { await db.close(); }
});

test('migrated signup trigger recognizes Google names and always assigns driver', async () => {
  const db = await fixture();
  try {
    await db.exec(migration);
    await db.query(`insert into auth.users (id, email, raw_user_meta_data) values
      ('44444444-4444-4444-8444-444444444444', 'google@example.test', $1),
      ('55555555-5555-4555-8555-555555555555', 'fallback@example.test', $2)`, [
      JSON.stringify({ full_name: '  Google Full Name  ', name: 'Ignored', role: 'admin' }),
      JSON.stringify({ full_name: ' ', name: '  Google Name  ', role: 'admin' }),
    ]);
    const { rows } = await db.query(`select role, display_name from public.crossflow_profiles
      where id not in ($1, $2, $3) order by id`, [DRIVER, OTHER, ADMIN]);
    assert.deepEqual(rows, [
      { role: 'driver', display_name: 'Google Full Name' },
      { role: 'driver', display_name: 'Google Name' },
    ]);
  } finally { await db.close(); }
});

test('an error before commit rolls back every function change and leaves existing data untouched', async () => {
  const db = await fixture();
  try {
    const before = await snapshot(db);
    const functionsBefore = await changedFunctions(db);
    const failingMigration = migration.replace('commit;', 'select 1 / 0;\ncommit;');
    await assert.rejects(db.exec(failingMigration), error => error.code === '22012');
    await db.exec('rollback');
    assert.deepEqual(await snapshot(db), before);
    assert.deepEqual(await changedFunctions(db), functionsBefore);
    assert.equal((await db.query('select schema_version from public.crossflow_auth_health()')).rows[0].schema_version, 1);
  } finally { await db.close(); }
});
