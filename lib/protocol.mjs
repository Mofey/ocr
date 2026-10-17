import { createHash, createHmac, timingSafeEqual } from 'node:crypto';

export const MAX_INPUT = 2 * 1024 * 1024;
export const MAX_OUTPUT = 1024 * 1024;

export class WorkerError extends Error {
  /** @param {number} status @param {string} code */
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}

export function configuration() {
  const secret = process.env.OCR_WORKER_GRANT_SECRET ?? '';
  const snapshotId = process.env.OCR_SANDBOX_SNAPSHOT_ID ?? '';
  const slot = process.env.OCR_SANDBOX_SLOT || 'ledgerical-ocr-active';
  const region = process.env.OCR_SANDBOX_REGION || 'iad1';
  if (Buffer.byteLength(secret) < 32 || !/^snap_[a-zA-Z0-9_-]+$/.test(snapshotId)
      || !/^[a-z0-9][a-z0-9-]{2,62}$/.test(slot) || !/^[a-z]{3}[0-9]$/.test(region)) {
    throw new WorkerError(503, 'OCR_WORKER_NOT_CONFIGURED');
  }
  return { secret, snapshotId, slot, region };
}

/** @param {unknown} condition @returns {asserts condition} */
function requireCondition(condition) {
  if (!condition) throw new WorkerError(401, 'INVALID_OCR_JOB_GRANT');
}

/** @param {string | null} token @param {string} secret */
export function verifyGrant(token, secret) {
  requireCondition(typeof token === 'string' && token.length <= 4096);
  const parts = token.split('.');
  requireCondition(parts.length === 2 && parts.every(p => /^[A-Za-z0-9_-]+$/.test(p)));
  const [encoded, signature] = parts;
  const actual = Buffer.from(signature, 'base64url');
  const expected = createHmac('sha256', secret).update(encoded, 'ascii').digest();
  requireCondition(actual.length === expected.length && timingSafeEqual(actual, expected));
  let grant;
  try { grant = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8')); }
  catch { throw new WorkerError(401, 'INVALID_OCR_JOB_GRANT'); }
  requireCondition(grant && typeof grant === 'object' && !Array.isArray(grant));
  const common = ['schemaVersion', 'audience', 'workspaceId', 'filename', 'contentType', 'sizeBytes', 'sha256', 'issuedAt', 'expiresAt'];
  const specific = grant.schemaVersion === 2 ? ['purpose', 'requestId'] : ['jobId', 'documentId', 'versionId', 'objectKey'];
  requireCondition([1, 2].includes(grant.schemaVersion)
    && Object.keys(grant).length === common.length + specific.length
    && [...common, ...specific].every(key => Object.hasOwn(grant, key)));
  requireCondition(grant.audience === 'document-ocr-worker');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  for (const field of ['workspaceId', ...(grant.schemaVersion === 2 ? ['requestId'] : ['jobId', 'documentId', 'versionId'])]) {
    requireCondition(typeof grant[field] === 'string' && uuid.test(grant[field]));
    grant[field] = grant[field].toLowerCase();
  }
  if (grant.schemaVersion === 2) requireCondition(grant.purpose === 'receipt-preview');
  else requireCondition(grant.objectKey === `immutable/workspaces/${grant.workspaceId}/documents/${grant.documentId}/versions/${grant.versionId}`);
  requireCondition(typeof grant.filename === 'string' && grant.filename.length <= 255);
  grant.filename = grant.filename.trim();
  requireCondition(grant.filename.length > 0 && !/[\\/\u0000]/.test(grant.filename));
  /** @type {Record<string, string>} */
  const formats = { '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' };
  const extension = /\.[^.]+$/.exec(grant.filename)?.[0].toLowerCase();
  requireCondition(typeof grant.contentType === 'string');
  grant.contentType = grant.contentType.trim().toLowerCase();
  requireCondition(extension !== undefined && Object.hasOwn(formats, extension) && formats[extension] === grant.contentType);
  requireCondition(Number.isSafeInteger(grant.sizeBytes) && grant.sizeBytes > 0 && grant.sizeBytes <= MAX_INPUT);
  requireCondition(typeof grant.sha256 === 'string' && /^[0-9a-fA-F]{64}$/.test(grant.sha256));
  grant.sha256 = grant.sha256.toLowerCase();
  const now = Math.floor(Date.now() / 1000);
  requireCondition(Number.isSafeInteger(grant.issuedAt) && grant.issuedAt > 0
    && Number.isSafeInteger(grant.expiresAt) && grant.expiresAt > now
    && grant.issuedAt <= now + 30 && grant.expiresAt > grant.issuedAt
    && grant.expiresAt - grant.issuedAt <= 120);
  return grant;
}

/** @param {Request} request @param {ReturnType<typeof verifyGrant>} grant @param {AbortSignal} signal */
export async function readInput(request, grant, signal) {
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/octet-stream'
      || !['', 'identity'].includes(request.headers.get('content-encoding') ?? '')) {
    throw new WorkerError(415, 'OCR_INPUT_CONTENT_TYPE_INVALID');
  }
  const length = request.headers.get('content-length');
  if (!length || !/^\d+$/.test(length)) throw new WorkerError(411, 'CONTENT_LENGTH_REQUIRED');
  if (Number(length) !== grant.sizeBytes) throw new WorkerError(413, 'OCR_INPUT_LIMIT');
  if (!request.body) throw new WorkerError(409, 'OCR_INPUT_SIZE_MISMATCH');
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    signal.throwIfAborted();
    for (;;) {
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      size += value.byteLength;
      if (size > grant.sizeBytes || size > MAX_INPUT) throw new WorkerError(413, 'OCR_INPUT_LIMIT');
      chunks.push(Buffer.from(value));
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (size !== grant.sizeBytes) throw new WorkerError(409, 'OCR_INPUT_SIZE_MISMATCH');
  const body = Buffer.concat(chunks, size);
  if (createHash('sha256').update(body).digest('hex') !== grant.sha256) throw new WorkerError(409, 'SOURCE_HASH_MISMATCH');
  return body;
}

/** @param {any} result @param {ReturnType<typeof verifyGrant>} grant */
export function verifyResult(result, grant) {
  const ids = grant.schemaVersion === 2 ? ['requestId'] : ['jobId', 'documentId', 'versionId'];
  if (!result || typeof result !== 'object' || result.schemaVersion !== grant.schemaVersion
      || result.workspaceId !== grant.workspaceId || ids.some(key => result[key] !== grant[key])
      || result.sourceSha256 !== grant.sha256 || result.sourceSizeBytes !== grant.sizeBytes
      || result.detectedContentType !== grant.contentType || result.contentValidated !== true
      || result.formatValidationStatus !== 'valid' || typeof result.text !== 'string'
      || result.text.length > 200000 || !Array.isArray(result.layoutLines)
      || result.layoutLines.length > 500 || result.ocr?.lineCount !== result.layoutLines.length) {
    throw new WorkerError(502, 'OCR_WORKER_RESULT_MISMATCH');
  }
  // The runner and Main additionally enforce the complete existing Pydantic schema.
  return result;
}

/** @param {number} status @param {unknown} body */
export function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), { status, headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff', ...(status === 429 ? { 'Retry-After': '2' } : {}),
  } });
}
