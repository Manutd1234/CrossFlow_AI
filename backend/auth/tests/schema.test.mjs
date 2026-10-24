/** Execute the committed schema in PostgreSQL, including its RLS and triggers.
 * Only Supabase's external auth.users/uid contract is stubbed; all CrossFlow SQL
 * runs unchanged. No remote project, credentials, or test accounts are used.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const DRIVER = '11111111-1111-4111-8111-111111111111';
const OTHER_DRIVER = '22222222-2222-4222-8222-222222222222';
const ADMIN = '33333333-3333-4333-8333-333333333333';
const LEGACY = '44444444-4444-4444-8444-444444444444';
const schema = await readFile(new URL('../schema.sql', import.meta.url), 'utf8');
const db = await PGlite.create();

before(async () => {
  await db.exec(`
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create schema auth;
    create table auth.users (
      id uuid primary key,
      email text,
      raw_user_meta_data jsonb not null default '{}'::jsonb
    );
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
    $$;
    grant usage on schema auth to authenticated, service_role;
    insert into auth.users (id, email, raw_user_meta_data) values (
      '${LEGACY}', 'legacy@example.test', '{"full_name":"  Legacy Google User  ","role":"admin"}'
    );
  `);
  await db.exec(schema);
  await db.query(`insert into auth.users (id, email, raw_user_meta_data) values
    ($1, 'driver@example.test', '{"role":"admin","display_name":"  Driver One  ","full_name":"Ignored Provider Name"}'),
    ($2, 'other@example.test', '{}'),
    ($3, 'admin@example.test', '{}')`, [DRIVER, OTHER_DRIVER, ADMIN]);
  await db.query('update public.crossflow_profiles set role = $1 where id = $2', ['admin', ADMIN]);
});

after(async () => { await db.close(); });

async function asUser(id, action) {
  await db.query(`select set_config('request.jwt.claim.sub', $1, false)`, [id]);
  await db.exec('set role authenticated');
  try {
    return await action();
  } finally {
    await db.exec('reset role');
    await db.exec(`reset "request.jwt.claim.sub"`);
  }
}

test('signup creates a driver profile and ignores a user-supplied admin role', async () => {
  const { rows } = await db.query('select id, role, display_name from public.crossflow_profiles where id = $1', [DRIVER]);
  assert.deepEqual(rows, [{ id: DRIVER, role: 'driver', display_name: 'Driver One' }]);
});

test('Google signup uses full_name and always creates a driver', async () => {
  const id = '55555555-5555-4555-8555-555555555555';
  await db.exec('begin');
  try {
    await db.query(
      `insert into auth.users (id, email, raw_user_meta_data) values ($1, $2, $3)`,
      [id, 'google.user@gmail.com', JSON.stringify({
        full_name: '  Google User  ', name: 'Fallback Name', role: 'admin',
      })],
    );
    const { rows } = await db.query(
      'select role, display_name from public.crossflow_profiles where id = $1', [id],
    );
    assert.deepEqual(rows, [{ role: 'driver', display_name: 'Google User' }]);
  } finally {
    await db.exec('rollback');
  }
});

test('Google signup falls back to name when full_name is blank', async () => {
  const id = '55555555-5555-4555-8555-555555555555';
  await db.exec('begin');
  try {
    await db.query(
      `insert into auth.users (id, email, raw_user_meta_data) values ($1, $2, $3)`,
      [id, 'google.user@gmail.com', JSON.stringify({
        display_name: ' ', full_name: ' ', name: '  Google Name  ', role: 'admin',
      })],
    );
    const { rows } = await db.query(
      'select role, display_name from public.crossflow_profiles where id = $1', [id],
    );
    assert.deepEqual(rows, [{ role: 'driver', display_name: 'Google Name' }]);
  } finally {
    await db.exec('rollback');
  }
});

test('schema backfills existing accounts and reruns without resetting an admin', async () => {
  await db.exec(schema);
  const { rows } = await db.query('select id, role, display_name from public.crossflow_profiles order by id');
  assert.equal(rows.length, 4);
  assert.equal(rows.find(row => row.id === LEGACY).role, 'driver');
  assert.equal(rows.find(row => row.id === LEGACY).display_name, 'Legacy Google User');
  assert.equal(rows.find(row => row.id === ADMIN).role, 'admin');
});

test('driver can read only their own profile', async () => {
  const { rows } = await asUser(DRIVER, () => db.query('select id from public.crossflow_profiles'));
  assert.deepEqual(rows, [{ id: DRIVER }]);
});

test('admin can read all profiles without policy recursion', async () => {
  const { rows } = await asUser(ADMIN, () => db.query('select id from public.crossflow_profiles'));
  assert.equal(rows.length, 4);
});

test('driver can update their display name', async () => {
  const { rows } = await asUser(DRIVER, () => db.query(
    'update public.crossflow_profiles set display_name = $1 where id = $2 returning display_name',
    ['Renamed Driver', DRIVER],
  ));
  assert.deepEqual(rows, [{ display_name: 'Renamed Driver' }]);
});

test('driver cannot promote themselves', async () => {
  await assert.rejects(asUser(DRIVER, () => db.query(
    `update public.crossflow_profiles set role = 'admin' where id = $1`, [DRIVER],
  )), error => error.code === '42501');
  const { rows } = await db.query('select role from public.crossflow_profiles where id = $1', [DRIVER]);
  assert.equal(rows[0].role, 'driver');
});

test('driver cannot rename or promote another driver', async () => {
  const { rows } = await asUser(DRIVER, () => db.query(
    `update public.crossflow_profiles set role = 'admin', display_name = 'Taken over' where id = $1 returning id`,
    [OTHER_DRIVER],
  ));
  assert.deepEqual(rows, []);
});

test('admin can promote and demote a driver', async () => {
  const promoted = await asUser(ADMIN, () => db.query(
    `update public.crossflow_profiles set role = 'admin' where id = $1 returning role`, [OTHER_DRIVER],
  ));
  assert.equal(promoted.rows[0].role, 'admin');
  const demoted = await asUser(ADMIN, () => db.query(
    `update public.crossflow_profiles set role = 'driver' where id = $1 returning role`, [OTHER_DRIVER],
  ));
  assert.equal(demoted.rows[0].role, 'driver');
});

test('profile IDs stay immutable, even for an admin', async () => {
  await assert.rejects(asUser(ADMIN, () => db.query(
    `update public.crossflow_profiles set id = '55555555-5555-4555-8555-555555555555' where id = $1`, [DRIVER],
  )), error => error.code === '23514');
});

test('authenticated callers cannot insert or delete profiles', async () => {
  await assert.rejects(asUser(DRIVER, () => db.query(
    `insert into public.crossflow_profiles (id, display_name) values ($1, 'Injected')`, [DRIVER],
  )), error => error.code === '42501');
  await assert.rejects(asUser(DRIVER, () => db.query(
    'delete from public.crossflow_profiles where id = $1', [DRIVER],
  )), error => error.code === '42501');
});

test('anonymous callers cannot read profiles or invoke role helpers', async () => {
  await db.exec('set role anon');
  try {
    await assert.rejects(db.query('select * from public.crossflow_profiles'), error => error.code === '42501');
    await assert.rejects(db.query('select public.crossflow_current_role()'), error => error.code === '42501');
    await assert.rejects(db.query('select public.crossflow_is_admin()'), error => error.code === '42501');
  } finally {
    await db.exec('reset role');
  }
});

test('profile is removed when the auth account is deleted', async () => {
  await db.query('delete from auth.users where id = $1', [LEGACY]);
  const { rows } = await db.query('select id from public.crossflow_profiles where id = $1', [LEGACY]);
  assert.deepEqual(rows, []);
});

test('health report confirms grants, RLS and the upgraded schema', async () => {
  const { rows } = await db.query('select * from public.crossflow_auth_health()');
  assert.deepEqual(rows, [{
    schema_version: 2, rls_enabled: true, authenticated_can_read: true, authenticated_cannot_insert: true,
  }]);
});
