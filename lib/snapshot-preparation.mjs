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
const connectionCodes = new Set([
  'ENOENT', 'EACCES', 'EPERM', 'ENOTDIR', 'EISDIR', 'ENOTFOUND', 'EAI_AGAIN',
  'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH',
  'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT', 'UND_ERR_SOCKET',
  'ERR_INVALID_URL', 'ERR_INVALID_ARG_TYPE', 'ERR_INVALID_ARG_VALUE', 'ERR_HTTP_INVALID_HEADER_VALUE',
  'UND_ERR_INVALID_ARG', 'CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'DEPTH_ZERO_SELF_SIGNED_CERT',
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'SELF_SIGNED_CERT_IN_CHAIN',
  'ERR_TLS_CERT_ALTNAME_INVALID', 'ERR_TLS_CERT_SIGNATURE_ALGORITHM_UNSUPPORTED',
  'ERR_SSL_WRONG_VERSION_NUMBER', 'ERR_SSL_TLSV1_ALERT_INTERNAL_ERROR',
]);
const certificateCodes = new Set([
  'CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'DEPTH_ZERO_SELF_SIGNED_CERT',
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'SELF_SIGNED_CERT_IN_CHAIN',
  'ERR_TLS_CERT_ALTNAME_INVALID', 'ERR_TLS_CERT_SIGNATURE_ALGORITHM_UNSUPPORTED',
]);

function failureChain(error) {
  const queue = [{ error, depth: 0 }];
  const seen = new Set();
  const chain = [];
  while (queue.length && chain.length < 12) {
    const entry = queue.shift();
    const current = entry.error;
    if (!current || typeof current !== 'object' || seen.has(current) || entry.depth > 4) continue;
    seen.add(current);
    chain.push(current);
    queue.push({ error: current.cause, depth: entry.depth + 1 });
    if (Array.isArray(current.errors)) {
      for (const child of current.errors.slice(0, 4)) queue.push({ error: child, depth: entry.depth + 1 });
    }
  }
  return chain;
}

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
  // fetch() wraps TLS/DNS/socket failures in TypeError and AggregateError.
  // Walk bounded causes instead of discarding the useful underlying code.
  for (const cause of failureChain(error)) {
    if (connectionCodes.has(cause.code)) facts.push(`code=${cause.code}`);
  }
  if (error instanceof SnapshotPreparationError && Number.isInteger(error.exitCode)) facts.push(`command_exit=${error.exitCode}`);
  if (!facts.length) {
    const name = ['TypeError', 'SyntaxError', 'TimeoutError', 'AbortError', 'APIError'].includes(error?.name) ? error.name : 'Error';
    facts.push(`error_type=${name}`);
  }
  return [...new Set(facts)].join(' ');
}

/** Fixed guidance only, selected using safe codes rather than raw messages. */
export function preparationFailureHint(error) {
  const chain = failureChain(error);
  if (chain.some(cause => certificateCodes.has(cause.code))) {
    return 'TLS certificate validation failed. Check this computer\'s clock and trusted certificate setup. On Node.js 24, use --use-system-ca for a trusted system certificate store, or configure NODE_EXTRA_CA_CERTS with the approved CA certificate. Keep certificate validation enabled.';
  }
  if (chain.some(cause => ['ENOTFOUND', 'EAI_AGAIN'].includes(cause.code))) {
    return 'Vercel\'s API could not be resolved. Check this computer\'s DNS, internet connection and required proxy settings.';
  }
  if (chain.some(cause => ['EACCES', 'EPERM'].includes(cause.code))) {
    return 'This process was denied access to a required file or connection. Check local permissions, firewall and proxy restrictions, and run from your normal terminal.';
  }
  if (chain.some(cause => [401, 403].includes(cause.response?.status))) {
    return 'Vercel rejected authentication or project access. Refresh your local login with vercel env pull .env.local and confirm the existing OCR project and team.';
  }
  if (chain.some(cause => ['ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH',
    'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT', 'UND_ERR_SOCKET'].includes(cause.code))) {
    return 'The Vercel API connection failed. Check this computer\'s internet connection and required firewall or proxy settings before retrying.';
  }
  return '';
}
