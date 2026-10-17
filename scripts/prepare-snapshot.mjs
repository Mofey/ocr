import { Sandbox } from '@vercel/sandbox';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { sdkAuth } from '../lib/sandbox.mjs';
import { loadLocalAuthentication } from './local-auth.mjs';
import './check.mjs';

if (!loadLocalAuthentication()) process.exit(1);

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const region = process.env.OCR_SANDBOX_REGION || 'iad1';
const manifest = JSON.parse(await readFile(resolve(root, 'vendor-manifest.json'), 'utf8'));
// Explicit allowlist: never upload .env files, project credentials, databases,
// receipts, parent repository files or a recursively selected working tree.
const sources = [...Object.keys(manifest.sha256),
  'sandbox/ocr_worker/vercel_runner.py', 'sandbox/Dockerfile',
  'sandbox/boot.sh', 'sandbox/process.sh', 'sandbox/prepare.sh'];
let sandbox;
let stage = 'create';
try {
  console.log('Preparing code and PaddleOCR models only. This creates billable Vercel Sandbox resources.');
  sandbox = await Sandbox.create({
    ...sdkAuth(), image: 'vercel/sandbox/universal',
    persistent: false, networkPolicy: 'allow-all', env: {}, ports: [],
    resources: { vcpus: 4 }, region, timeout: 45 * 60 * 1000,
  });
  stage = 'upload trusted source';
  await sandbox.mkDir('/vercel/sandbox/build/ocr_worker');
  await sandbox.writeFiles(await Promise.all(sources.map(async source => ({
    path: `/vercel/sandbox/build/${source.slice('sandbox/'.length)}`,
    content: await readFile(resolve(root, source)), mode: source.endsWith('.sh') ? 0o755 : 0o644,
  }))));
  stage = 'install Docker and build PaddleOCR image';
  console.log('Installing Docker, pinned Python packages and OCR models. This can take several minutes.');
  const build = await sandbox.runCommand({
    cmd: '/vercel/sandbox/build/prepare.sh', sudo: true,
    stdout: process.stdout, stderr: process.stderr,
  });
  if (build.exitCode !== 0) throw new Error('build failed');
  // Freeze egress BEFORE the runtime guard runs. No documents have been sent.
  stage = 'enforce offline runtime restrictions';
  await sandbox.update({ networkPolicy: 'deny-all' });
  const probe = await sandbox.runCommand({ cmd: '/opt/ledgerical/boot.sh', sudo: true });
  if (probe.exitCode !== 0) throw new Error('runtime restrictions unavailable');
  stage = 'clean temporary bootstrap state';
  const clean = await sandbox.runCommand({
    cmd: 'sh', sudo: true, args: ['-ec', `
      umount /run/ledgerical-ocr
      rmdir /run/ledgerical-ocr
      kill "$(cat /var/run/docker.pid)"
      attempt=0
      while docker info >/dev/null 2>&1; do
        attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || exit 1; sleep 0.1
      done
      rm -f /var/run/docker.pid /var/run/docker.sock
    `],
  });
  if (clean.exitCode !== 0) throw new Error('cleanup failed');
  stage = 'snapshot trusted code and models';
  const snapshot = await sandbox.snapshot({ expiration: 0 });
  console.log(`OCR_SANDBOX_SNAPSHOT_ID=${snapshot.snapshotId}`);
  console.log(`OCR_SANDBOX_REGION=${region}`);
  console.log('Set these on this Vercel project, then redeploy. No user file was processed or snapshotted.');
} catch {
  // SDK errors may carry auth/provider details; emit the safe stage only.
  console.error(`Preparation failed during: ${stage}. No deployment settings were changed.`);
  process.exitCode = 1;
} finally {
  if (sandbox) {
    try { await sandbox.stop({ signal: AbortSignal.timeout(15000) }); } catch { /* delete next */ }
    try { await sandbox.delete({ signal: AbortSignal.timeout(15000) }); }
    catch { console.error('Preparation sandbox cleanup incomplete; delete it in the Vercel dashboard.'); process.exitCode = 1; }
  }
}
