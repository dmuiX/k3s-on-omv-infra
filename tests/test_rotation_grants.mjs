// Opt-in SQL execution on an in-memory PostgreSQL 18 engine. See tests/README.md.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const [prefix, sqlPath] = process.argv.slice(2);
assert.ok(prefix && sqlPath, 'Usage: node tests/test_rotation_grants.mjs <npm-prefix> <grants.sql>');
const { PGlite } = createRequire(resolve(prefix, 'package.json'))('@electric-sql/pglite');
const migration = readFileSync(sqlPath, 'utf8');

async function inMemory(test) {
  const db = new PGlite();
  try {
    await db.exec('CREATE ROLE openbao_manager LOGIN CREATEROLE');
    await test(db);
  } finally {
    await db.close();
  }
}

async function memberships(db) {
  return (await db.query(`
    SELECT pg_get_userbyid(roleid) AS target, pg_get_userbyid(grantor) AS grantor,
           admin_option, inherit_option, set_option
    FROM pg_auth_members WHERE member = 'openbao_manager'::regrole
    ORDER BY target, grantor
  `)).rows;
}

async function effectiveAccess(db, login) {
  const { rows: [access] } = await db.query(`
    SELECT pg_has_role('openbao_manager', $1::name, 'USAGE') AS has_usage,
           pg_has_role('openbao_manager', $1::name, 'SET') AS can_set
  `, [login]);
  return access;
}

await inMemory(async (db) => {
  const { rows: [{ server_version: version }] } = await db.query('SHOW server_version');
  assert.match(version, /^18\./);
  console.log(`PostgreSQL ${version} (in-memory PGlite)`);

  await db.exec(`
    CREATE ROLE ordinary LOGIN;
    CREATE ROLE grafana LOGIN PASSWORD 'synthetic-test-only';
    CREATE TEMP TABLE original_verifier AS
      SELECT rolpassword FROM pg_authid WHERE rolname = 'grafana';
    GRANT grafana TO openbao_manager WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;
  `);
  await db.exec('CREATE DATABASE ordinary OWNER ordinary');
  await db.exec('CREATE DATABASE grafana OWNER grafana');
  assert.deepEqual(await effectiveAccess(db, 'grafana'), { has_usage: true, can_set: true });

  const expected = ['grafana', 'radar'].map((target) => ({
    target, grantor: 'postgres', admin_option: true, inherit_option: false, set_option: false,
  }));
  for (let pass = 0; pass < 2; pass++) {
    await db.exec(migration);
    assert.deepEqual(await memberships(db), expected);
    for (const login of ['grafana', 'radar']) {
      assert.deepEqual(await effectiveAccess(db, login), { has_usage: false, can_set: false });
    }
    const { rows } = await db.query(`
      SELECT r.rolpassword IS NOT NULL AND r.rolpassword = v.rolpassword AS unchanged
      FROM pg_authid r CROSS JOIN original_verifier v WHERE r.rolname = 'grafana'
    `);
    assert.deepEqual(rows, [{ unchanged: true }]);
  }
  console.log('PASS: direct options reconciled; repeat application preserves synthetic password; no ordinary grant');
});

for (const [label, setup, hasUsage] of [
  ['transitive USAGE', `
    CREATE ROLE bridge;
    GRANT radar TO bridge WITH INHERIT TRUE, SET FALSE;
    GRANT bridge TO openbao_manager WITH INHERIT TRUE, SET FALSE;
  `, true],
  ['SET to intermediary then USAGE', `
    CREATE ROLE bridge;
    GRANT radar TO bridge WITH INHERIT TRUE, SET FALSE;
    GRANT bridge TO openbao_manager WITH INHERIT FALSE, SET TRUE;
  `, false],
  ['conflicting direct grantor', `
    CREATE ROLE other_admin CREATEROLE;
    GRANT radar TO other_admin WITH ADMIN TRUE;
    SET ROLE other_admin;
    GRANT radar TO openbao_manager WITH INHERIT TRUE, SET FALSE;
    RESET ROLE;
  `, true],
]) {
  await inMemory(async (db) => {
    await db.exec('CREATE ROLE radar LOGIN');
    await db.exec(setup);
    assert.deepEqual(await effectiveAccess(db, 'radar'), { has_usage: hasUsage, can_set: false });
    const before = await memberships(db);
    await assert.rejects(db.exec(migration), /Unexpected or conflicting grant for radar/);
    await db.exec('ROLLBACK');
    assert.deepEqual(await memberships(db), before);
    // grafana sorts before radar, so its newly created role must also roll back.
    const { rows } = await db.query("SELECT 1 FROM pg_roles WHERE rolname = 'grafana'");
    assert.deepEqual(rows, []);
  });
  console.log(`PASS: ${label} refused with atomic rollback`);
}
