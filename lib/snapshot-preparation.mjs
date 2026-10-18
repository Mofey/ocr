import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import { posix, resolve, sep } from 'node:path';

export const SNAPSHOT_BUILD_DIRECTORY = '/vercel/sandbox/build';
const extraSources = [
  'sandbox/ocr_worker/vercel_runner.py', 'sandbox/Dockerfile',
  'sandbox/boot.sh', 'sandbox/process.sh', 'sandbox/prepare.sh',
];
const failures = new Set([
  'SNAPSHOT_SOURCE_PATH_INVALID', 'SNAPSHOT_SOURCE_HASH_INVALID',
  'SNAPSHOT_SOURCE_DIRECTORY_FAILED', 'SNAPSHOT_SOURCE_VERIFY_FAILED',
]);

export class SnapshotPreparationError extends Error {
  constructor(code, exitCode) {
    super(code);
    this.code = code;
    this.exitCode = exitCode;
  }
}

/** Load and validate the complete explicit source allowlist before allocating a VM. */
export async function readTrustedSnapshotFiles(root) {
  const canonicalRoot = await realpath(root);
  const manifest = JSON.parse(await readFile(resolve(root, 'vendor-manifest.json'), 'utf8'));
  const hashes = manifest.sha256;
  if (!hashes || typeof hashes !== 'object' || Array.isArray(hashes)) {
    throw new SnapshotPreparationError('SNAPSHOT_SOURCE_HASH_INVALID');
  }
  for (const source of Object.keys(hashes)) {
    if (!/^sandbox\/ocr_worker\/(?:[A-Za-z0-9_]+\/)*[A-Za-z0-9_]+\.py$/.test(source)
        && !['sandbox/paddle-dependencies.in', 'sandbox/paddle-dependencies-linux.lock'].includes(source)) {
      throw new SnapshotPreparationError('SNAPSHOT_SOURCE_PATH_INVALID');
    }
  }
  const sources = [...new Set([...Object.keys(hashes), ...extraSources])];
  return Promise.all(sources.map(async source => {
    const path = await realpath(resolve(root, source));
    if (!path.startsWith(`${canonicalRoot}${sep}sandbox${sep}`)) {
      throw new SnapshotPreparationError('SNAPSHOT_SOURCE_PATH_INVALID');
    }
    let content = await readFile(path);
    if (Object.hasOwn(hashes, source) && createHash('sha256').update(content).digest('hex') !== hashes[source]) {
      throw new SnapshotPreparationError('SNAPSHOT_SOURCE_HASH_INVALID');
    }
    // Windows checkouts must still produce executable Linux shell scripts.
    if (source.endsWith('.sh')) content = Buffer.from(content.toString('utf8').replace(/\r\n/g, '\n'));
    return {
      path: `${SNAPSHOT_BUILD_DIRECTORY}/${source.slice('sandbox/'.length)}`,
      content, mode: source.endsWith('.sh') ? 0o755 : 0o644,
    };
  }));
}

export async function uploadTrustedSnapshotFiles(sandbox, files, onStage = () => {}) {
  onStage('create upload directories');
  // mkDir() is not recursive. mkdir -p creates the absent build parent too.
  const directories = [...new Set(files.map(file => posix.dirname(file.path)))];
  const directory = await sandbox.runCommand({
    cmd: 'mkdir', args: ['-p', ...directories],
    signal: AbortSignal.timeout(30000),
  });
  if (directory.exitCode !== 0) {
    throw new SnapshotPreparationError('SNAPSHOT_SOURCE_DIRECTORY_FAILED', directory.exitCode);
  }
  onStage('upload trusted source');
  await sandbox.writeFiles(files, { signal: AbortSignal.timeout(30000) });

  onStage('verify uploaded source');
  // Arguments contain only allowlisted source paths. No credentials or documents.
  const verification = await sandbox.runCommand({
    cmd: 'sh', args: ['-ec', [
      'for file do',
      '  test -r "$file"',
      '  case "$file" in *.sh) test -x "$file" ;; esac',
      'done',
      'sha256sum "$@"',
    ].join('\n'), 'verify-snapshot-source', ...files.map(file => file.path)],
    signal: AbortSignal.timeout(30000),
  });
  if (verification.exitCode !== 0) {
    throw new SnapshotPreparationError('SNAPSHOT_SOURCE_VERIFY_FAILED', verification.exitCode);
  }
  const expected = files.map(file => `${createHash('sha256').update(file.content).digest('hex')}  ${file.path}`).join('\n');
  if ((await verification.stdout()).trim() !== expected) {
    throw new SnapshotPreparationError('SNAPSHOT_SOURCE_VERIFY_FAILED');
  }
}

/** Emit diagnostic facts without SDK response bodies, tokens, URLs or raw messages. */
export function safePreparationFailure(error) {
  const facts = [];
  if (error instanceof SnapshotPreparationError && failures.has(error.code)) facts.push(`code=${error.code}`);
  const status = error?.response?.status;
  if (Number.isInteger(status) && status >= 400 && status <= 599) facts.push(`http_status=${status}`);
  const code = error?.code || error?.cause?.code;
  if (['ENOENT', 'EACCES', 'EPERM', 'ENOTDIR', 'EISDIR', 'ENOTFOUND', 'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED'].includes(code)) {
    facts.push(`code=${code}`);
  }
  if (error instanceof SnapshotPreparationError && Number.isInteger(error.exitCode)) facts.push(`command_exit=${error.exitCode}`);
  if (!facts.length) {
    const name = ['TypeError', 'SyntaxError', 'TimeoutError', 'AbortError', 'APIError'].includes(error?.name) ? error.name : 'Error';
    facts.push(`error_type=${name}`);
  }
  return facts.join(' ');
}
