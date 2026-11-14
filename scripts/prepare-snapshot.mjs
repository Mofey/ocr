import { Sandbox } from '@vercel/sandbox';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { boundedFile, parserFailureDetails, sdkAuth } from '../lib/sandbox.mjs';
import { bootstrapFailureDetails } from '../lib/bootstrap-diagnostics.mjs';
import { loadLocalAuthentication } from './local-auth.mjs';
import { preparationFailureHint, readTrustedSnapshotFiles, safePreparationFailure, uploadTrustedSnapshotFiles } from '../lib/snapshot-preparation.mjs';
import './check.mjs';

if (!loadLocalAuthentication()) process.exit(1);

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const region = process.env.OCR_SANDBOX_REGION || 'iad1';
let sandbox;
let stage = 'read trusted local source';
try {
  // Missing/changed files must fail before a billable sandbox is created.
  const files = await readTrustedSnapshotFiles(root);
  stage = 'create';
  console.log('Preparing code and PaddleOCR models only. This creates billable Vercel Sandbox resources.');
  sandbox = await Sandbox.create({
    ...sdkAuth(), image: 'vercel/sandbox/universal',
    persistent: false, networkPolicy: 'allow-all', env: {}, ports: [],
    resources: { vcpus: 4 }, region, timeout: 45 * 60 * 1000,
    signal: AbortSignal.timeout(60000),
  });
  await uploadTrustedSnapshotFiles(sandbox, files, value => { stage = value; });
  stage = 'check cgroup prerequisites';
  console.log('Checking cgroup support before installing Docker and OCR dependencies.');
  const cgroups = await sandbox.runCommand({
    cmd: '/vercel/sandbox/build/boot.sh', args: ['--check-cgroups'], sudo: true,
    signal: AbortSignal.timeout(15000),
  });
  if (cgroups.exitCode !== 0) {
    console.error('Cgroup prerequisite check failed:', bootstrapFailureDetails(await cgroups.stderr(), cgroups.exitCode));
    throw new Error('cgroup prerequisites unavailable');
  }
  stage = 'install Docker and build PaddleOCR image';
  console.log('Installing Docker, pinned Python packages and OCR models. This can take several minutes.');
  const build = await sandbox.runCommand({
    cmd: '/vercel/sandbox/build/prepare.sh', sudo: true,
    stdout: process.stdout, stderr: process.stderr,
  });
  if (build.exitCode !== 0) throw new Error('build failed');
  // Freeze egress BEFORE the runtime guard runs. No documents have been sent.
  stage = 'enforce offline runtime restrictions';
  console.log('Checking the offline runtime restrictions.');
  await sandbox.update({ networkPolicy: 'deny-all' }, { signal: AbortSignal.timeout(30000) });
  const probe = await sandbox.runCommand({ cmd: '/opt/ledgerical/boot.sh', sudo: true, signal: AbortSignal.timeout(30000) });
  if (probe.exitCode !== 0) {
    console.error('Runtime check failed:', bootstrapFailureDetails(await probe.stderr(), probe.exitCode));
    throw new Error('runtime restrictions unavailable');
  }
  stage = 'check offline image recognition readiness';
  console.log('Checking prepared models using generated JPEG content under the production job limits.');
  const models = await sandbox.runCommand({
    cmd: '/opt/ledgerical/process.sh', args: ['--model-probe'], sudo: true,
    signal: AbortSignal.timeout(35000),
  });
  if (models.exitCode !== 0) throw new Error('offline image recognition unavailable');
  const readiness = await boundedFile(sandbox, AbortSignal.timeout(5000), 2048);
  if (readiness.status !== 200 || readiness.ready !== true) {
    console.error('Offline image recognition unavailable:', parserFailureDetails(readiness.diagnostic));
    throw new Error('offline image recognition unavailable');
  }
  stage = 'clean temporary bootstrap state';
  const clean = await sandbox.runCommand({
    cmd: 'sh', sudo: true, signal: AbortSignal.timeout(20000), args: ['-ec', `
      umount /run/ledgerical-ocr
      rmdir /run/ledgerical-ocr
      daemon_pid=$(cat /var/run/docker.pid)
      kill "$daemon_pid"
      attempt=0
      while kill -0 "$daemon_pid" 2>/dev/null; do
        attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || exit 1; sleep 0.1
      done
      rm -f /var/run/docker.pid /var/run/docker.sock
    `],
  });
  if (clean.exitCode !== 0) throw new Error('cleanup failed');
  stage = 'snapshot trusted code and models';
  console.log('Saving the prepared code and model snapshot.');
  const snapshot = await sandbox.snapshot({ expiration: 0, signal: AbortSignal.timeout(180000) });
  console.log(`OCR_SANDBOX_SNAPSHOT_ID=${snapshot.snapshotId}`);
  console.log(`OCR_SANDBOX_REGION=${region}`);
  console.log('Set these on this Vercel project, then redeploy. No user file was processed or snapshotted.');
} catch (error) {
  console.error(`Preparation failed during: ${stage}. ${safePreparationFailure(error)}. No deployment settings were changed.`);
  const hint = preparationFailureHint(error);
  if (hint) console.error(hint);
  process.exitCode = 1;
} finally {
  if (sandbox) {
    try { await sandbox.stop({ signal: AbortSignal.timeout(15000) }); } catch { /* delete next */ }
    try { await sandbox.delete({ signal: AbortSignal.timeout(15000) }); }
    catch { console.error('Preparation sandbox cleanup incomplete; delete it in the Vercel dashboard.'); process.exitCode = 1; }
  }
}
