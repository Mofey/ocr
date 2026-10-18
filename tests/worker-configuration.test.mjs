import assert from 'node:assert/strict';
import test from 'node:test';
import health from '../api/health.mjs';
import { configuration } from '../lib/protocol.mjs';

const keys = ['OCR_WORKER_GRANT_SECRET', 'OCR_SANDBOX_SNAPSHOT_ID', 'OCR_SANDBOX_SLOT', 'OCR_SANDBOX_REGION'];
const secret = 'local-test-secret-with-at-least-32-characters';

function settings(t, overrides = {}) {
  const previous = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  t.after(() => {
    for (const key of keys) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  });
  for (const key of keys) delete process.env[key];
  Object.assign(process.env, {
    OCR_WORKER_GRANT_SECRET: secret,
    OCR_SANDBOX_SNAPSHOT_ID: 'snap_local_test_fixture',
  }, overrides);
}

for (const key of ['OCR_WORKER_GRANT_SECRET', 'OCR_SANDBOX_SNAPSHOT_ID']) {
  test(`health identifies missing ${key} without exposing configuration values`, async t => {
    settings(t, { [key]: '' });
    const response = health.fetch(new Request('https://worker.invalid/healthz'));
    assert.equal(response.status, 503);
    const body = await response.text();
    assert.deepEqual(JSON.parse(body).detail, { error: 'OCR_WORKER_NOT_CONFIGURED', missingOrInvalid: [key] });
    assert.ok(!body.includes(secret));
    assert.ok(!body.includes('snap_local_test_fixture'));
    assert.throws(configuration, { code: 'OCR_WORKER_NOT_CONFIGURED' });
  });
}

test('health identifies invalid snapshot, slot and region while parser access stays disabled', async t => {
  settings(t, { OCR_SANDBOX_SNAPSHOT_ID: 'not-a-snapshot', OCR_SANDBOX_SLOT: 'Bad Slot!', OCR_SANDBOX_REGION: 'invalid' });
  const response = health.fetch(new Request('https://worker.invalid/healthz'));
  assert.equal(response.status, 503);
  assert.deepEqual((await response.json()).detail.missingOrInvalid, ['OCR_SANDBOX_SNAPSHOT_ID', 'OCR_SANDBOX_SLOT', 'OCR_SANDBOX_REGION']);
  assert.throws(configuration, { code: 'OCR_WORKER_NOT_CONFIGURED' });
});

test('configured health is explicit that inference has not been verified', async t => {
  settings(t);
  const response = health.fetch(new Request('https://worker.invalid/healthz'));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).inferenceVerified, false);
  assert.equal(response.headers.get('cache-control'), 'no-store');
});
