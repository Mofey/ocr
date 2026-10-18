import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, posix } from 'node:path';
import test from 'node:test';
import {
  readTrustedSnapshotFiles, safePreparationFailure, SnapshotPreparationError,
  uploadTrustedSnapshotFiles,
} from '../lib/snapshot-preparation.mjs';

const digest = content => createHash('sha256').update(content).digest('hex');

async function sourceFixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'ocr-snapshot-source-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sources = {
    'sandbox/ocr_worker/__init__.py': '# trusted parser\n',
    'sandbox/ocr_worker/vercel_runner.py': '# trusted entry point\n',
    'sandbox/paddle-dependencies.in': 'test-dependency==1.0\n',
    'sandbox/paddle-dependencies-linux.lock': 'test-dependency==1.0\n',
    'sandbox/Dockerfile': 'FROM python:3.12-slim\n',
    'sandbox/boot.sh': '#!/bin/sh\r\nexit 0\r\n',
    'sandbox/process.sh': '#!/bin/sh\r\nexit 0\r\n',
    'sandbox/prepare.sh': '#!/bin/sh\r\nexit 0\r\n',
  };
  for (const [path, content] of Object.entries(sources)) {
    await mkdir(dirname(join(root, path)), { recursive: true });
    await writeFile(join(root, path), content);
  }
  const manifest = { sha256: Object.fromEntries(
    ['sandbox/ocr_worker/__init__.py', 'sandbox/paddle-dependencies.in', 'sandbox/paddle-dependencies-linux.lock']
      .map(path => [path, digest(sources[path])]),
  ) };
  await writeFile(join(root, 'vendor-manifest.json'), JSON.stringify(manifest));
  await writeFile(join(root, '.env.local'), 'PRIVATE_TEST_CREDENTIAL=must-never-upload');
  await writeFile(join(root, 'private-receipt.pdf'), 'must-never-upload');
  return { root, manifest };
}

function uploadSandbox({ mkdirExit = 0, verificationExit = 0, corrupt = false, writeError } = {}) {
  const directories = new Set(['/vercel/sandbox']);
  const remote = new Map();
  return {
    calls: [], remote,
    async mkDir() { throw Object.assign(new Error('parent directory missing'), { code: 'ENOENT' }); },
    async runCommand(command) {
      this.calls.push(command);
      if (command.cmd === 'mkdir') {
        assert.equal(command.args[0], '-p');
        if (mkdirExit === 0) {
          for (const directory of command.args.slice(1)) {
            let path = directory;
            while (path !== '/') { directories.add(path); path = posix.dirname(path); }
          }
        }
        return { exitCode: mkdirExit };
      }
      assert.equal(command.cmd, 'sh');
      assert.match(command.args[1], /test -x/);
      assert.match(command.args[1], /sha256sum/);
      const paths = command.args.slice(3);
      const output = paths.map(path => `${digest(remote.get(path).content)}  ${path}`).join('\n');
      return { exitCode: verificationExit, stdout: async () => `${output}\n` };
    },
    async writeFiles(files) {
      if (writeError) throw writeError;
      for (const file of files) {
        assert.ok(directories.has(posix.dirname(file.path)), 'upload directory must exist before writing');
        remote.set(file.path, { ...file, content: Buffer.from(file.content) });
      }
      if (corrupt) remote.values().next().value.content = Buffer.from('corrupt transfer');
    },
  };
}

test('trusted source preflight excludes credentials and customer files and normalizes shell line endings', async t => {
  const { root } = await sourceFixture(t);
  const files = await readTrustedSnapshotFiles(root);
  assert.equal(files.length, 8);
  assert.ok(files.every(file => file.path.startsWith('/vercel/sandbox/build/')));
  assert.ok(files.every(file => !file.content.includes('must-never-upload')));
  for (const file of files.filter(file => file.path.endsWith('.sh'))) {
    assert.equal(file.mode, 0o755);
    assert.ok(!file.content.includes('\r'));
    assert.ok(file.content.toString().startsWith('#!/bin/sh\n'));
  }
});

test('missing and changed trusted files fail in local preflight', async t => {
  const { root } = await sourceFixture(t);
  await writeFile(join(root, 'sandbox/ocr_worker/__init__.py'), 'changed source');
  await assert.rejects(readTrustedSnapshotFiles(root), { code: 'SNAPSHOT_SOURCE_HASH_INVALID' });
  await rm(join(root, 'sandbox/ocr_worker/__init__.py'));
  await assert.rejects(readTrustedSnapshotFiles(root), { code: 'ENOENT' });
});

test('a source manifest cannot upload a local credential by traversal or an extra path', async t => {
  const { root, manifest } = await sourceFixture(t);
  for (const path of ['sandbox/../.env.local', '.env.local', 'sandbox/.env.local']) {
    await writeFile(join(root, 'vendor-manifest.json'), JSON.stringify({ sha256: { ...manifest.sha256, [path]: 'a'.repeat(64) } }));
    await assert.rejects(readTrustedSnapshotFiles(root), { code: 'SNAPSHOT_SOURCE_PATH_INVALID' });
  }
});

test('a fresh sandbox gets all parent directories before upload, and uploaded contents are verified', async t => {
  const { root } = await sourceFixture(t);
  const files = await readTrustedSnapshotFiles(root);
  const sandbox = uploadSandbox();
  const stages = [];
  await uploadTrustedSnapshotFiles(sandbox, files, stage => stages.push(stage));
  assert.deepEqual(stages, ['create upload directories', 'upload trusted source', 'verify uploaded source']);
  assert.equal(sandbox.remote.size, files.length);
  assert.equal(sandbox.remote.get('/vercel/sandbox/build/prepare.sh').mode, 0o755);
});

test('directory failure stops before any upload', async t => {
  const { root } = await sourceFixture(t);
  const sandbox = uploadSandbox({ mkdirExit: 1 });
  await assert.rejects(uploadTrustedSnapshotFiles(sandbox, await readTrustedSnapshotFiles(root)), { code: 'SNAPSHOT_SOURCE_DIRECTORY_FAILED', exitCode: 1 });
  assert.equal(sandbox.remote.size, 0);
});

test('failed uploads remain distinguishable from directory or verification failures', async t => {
  const { root } = await sourceFixture(t);
  const failure = Object.assign(new Error('private provider details'), { response: { status: 403 } });
  const stages = [];
  await assert.rejects(uploadTrustedSnapshotFiles(uploadSandbox({ writeError: failure }), await readTrustedSnapshotFiles(root), stage => stages.push(stage)), error => error === failure);
  assert.equal(stages.at(-1), 'upload trusted source');
  assert.equal(safePreparationFailure(failure), 'http_status=403');
});

test('source corruption or verification failure blocks model preparation', async t => {
  const { root } = await sourceFixture(t);
  const files = await readTrustedSnapshotFiles(root);
  await assert.rejects(uploadTrustedSnapshotFiles(uploadSandbox({ corrupt: true }), files), { code: 'SNAPSHOT_SOURCE_VERIFY_FAILED' });
  await assert.rejects(uploadTrustedSnapshotFiles(uploadSandbox({ verificationExit: 1 }), files), { code: 'SNAPSHOT_SOURCE_VERIFY_FAILED', exitCode: 1 });
});

test('diagnostics show only safe codes and never raw exception or provider response data', () => {
  const error = Object.assign(new Error('secret-token-should-not-print'), { response: { status: 500 }, json: { token: 'secret-token-should-not-print' } });
  assert.equal(safePreparationFailure(error), 'http_status=500');
  assert.equal(safePreparationFailure(Object.assign(new Error('sensitive path'), { code: 'ENOENT' })), 'code=ENOENT');
  assert.equal(safePreparationFailure(Object.assign(new Error('sensitive details'), { code: 'private_token' })), 'error_type=Error');
  assert.equal(safePreparationFailure(new SnapshotPreparationError('SNAPSHOT_SOURCE_DIRECTORY_FAILED', 2)), 'code=SNAPSHOT_SOURCE_DIRECTORY_FAILED command_exit=2');
});
