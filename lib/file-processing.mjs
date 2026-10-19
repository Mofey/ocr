import { createHash, createHmac, timingSafeEqual } from 'node:crypto';
import { WorkerError } from './protocol.mjs';

export const FILE_MAX_INPUT = 10 * 1024 * 1024;
export const FILE_MAX_OUTPUT = 3 * 1024 * 1024;
const purposes = new Set(['validate', 'tabular', 'signature-image', 'public-image']);
const formats = { '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.csv': 'text/csv', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const fail = () => { throw new WorkerError(401, 'INVALID_FILE_JOB_GRANT'); };

export function verifyFileGrant(token, secret) {
  if (typeof token !== 'string' || token.length > 8192) fail();
  const parts = token.split('.');
  if (parts.length !== 2 || parts.some(p => !/^[A-Za-z0-9_-]+$/.test(p))) fail();
  const signature = Buffer.from(parts[1], 'base64url');
  const expected = createHmac('sha256', secret).update(parts[0], 'ascii').digest();
  if (signature.length !== expected.length || !timingSafeEqual(signature, expected)) fail();
  let g;
  try { g = JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8')); } catch { fail(); }
  const fields = ['schemaVersion', 'audience', 'workspaceId', 'requestId', 'purpose', 'filename', 'contentType', 'sizeBytes', 'sha256', 'sourceUrl', 'issuedAt', 'expiresAt'];
  if (!g || typeof g !== 'object' || Object.keys(g).length !== fields.length || fields.some(k => !Object.hasOwn(g, k))) fail();
  if (g.schemaVersion !== 3 || g.audience !== 'document-file-worker' || !purposes.has(g.purpose)) fail();
  if (typeof g.workspaceId !== 'string' || typeof g.requestId !== 'string' || !uuid.test(g.workspaceId) || !uuid.test(g.requestId)) fail();
  if (typeof g.filename !== 'string' || !g.filename.trim() || g.filename.length > 255 || /[\\/\u0000-\u001f]/.test(g.filename)) fail();
  const ext = /\.[^.]+$/.exec(g.filename)?.[0].toLowerCase();
  if (!ext || formats[ext] !== g.contentType) fail();
  const limit = ['.pdf', '.docx', '.xlsx'].includes(ext) ? FILE_MAX_INPUT : 5 * 1024 * 1024;
  if (!Number.isSafeInteger(g.sizeBytes) || g.sizeBytes < 1 || g.sizeBytes > limit || !/^[a-f0-9]{64}$/.test(g.sha256)) fail();
  if (g.purpose === 'tabular' && !['.csv', '.xlsx'].includes(ext)) fail();
  if (['signature-image', 'public-image'].includes(g.purpose) && !g.contentType.startsWith('image/')) fail();
  if (typeof g.sourceUrl !== 'string' || g.sourceUrl.length > 4096) fail();
  if (!g.sourceUrl && g.sizeBytes > 2 * 1024 * 1024) fail();
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(g.issuedAt) || !Number.isSafeInteger(g.expiresAt) || g.issuedAt > now + 30 || g.issuedAt < now - 120 || g.expiresAt <= now || g.expiresAt <= g.issuedAt || g.expiresAt - g.issuedAt > 120) fail();
  if (g.sourceUrl) {
    // Only the trusted gateway fetches opaque bytes. The parser has no network,
    // URLs or storage credentials. Restrict even signed URLs to our R2 origin.
    let url, origin;
    try { url = new URL(g.sourceUrl); origin = new URL(process.env.FILE_PROCESSOR_SOURCE_ORIGIN || ''); } catch { throw new WorkerError(503, 'FILE_SOURCE_ORIGIN_NOT_CONFIGURED'); }
    if (url.protocol !== 'https:' || url.origin !== origin.origin || url.username || url.password || url.hash || origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash || !url.pathname.includes(`/workspaces/${g.workspaceId}/`)) fail();
  }
  return g;
}

async function boundedBody(stream, maximum, signal) {
  if (!stream) throw new WorkerError(409, 'OCR_INPUT_SIZE_MISMATCH');
  const reader = stream.getReader(), chunks = [];
  let size = 0;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    for (;;) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw new WorkerError(413, 'OCR_INPUT_LIMIT');
      chunks.push(Buffer.from(value));
    }
  } finally {
    signal.removeEventListener('abort', abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (size !== maximum) throw new WorkerError(409, 'OCR_INPUT_SIZE_MISMATCH');
  return Buffer.concat(chunks, size);
}

export async function readFileInput(request, grant, signal) {
  let body;
  if (grant.sourceUrl) {
    if (request.headers.get('content-length') !== '0') throw new WorkerError(400, 'FILE_REQUEST_BODY_INVALID');
    if (request.body) await boundedBody(request.body, 0, signal);
    const source = await fetch(grant.sourceUrl, { redirect: 'error', signal, headers: { 'Accept-Encoding': 'identity' } });
    if (source.status !== 200 || source.headers.get('content-length') !== String(grant.sizeBytes) || !['', 'identity'].includes(source.headers.get('content-encoding') ?? '')) {
      await source.body?.cancel();
      throw new WorkerError(409, 'FILE_SOURCE_MISMATCH');
    }
    body = await boundedBody(source.body, grant.sizeBytes, signal);
  } else {
    if (request.headers.get('content-type') !== 'application/octet-stream' || request.headers.get('content-length') !== String(grant.sizeBytes) || !['', 'identity'].includes(request.headers.get('content-encoding') ?? '')) throw new WorkerError(400, 'FILE_REQUEST_BODY_INVALID');
    body = await boundedBody(request.body, grant.sizeBytes, signal);
  }
  if (createHash('sha256').update(body).digest('hex') !== grant.sha256) { body.fill(0); throw new WorkerError(409, 'SOURCE_HASH_MISMATCH'); }
  return body;
}

export function verifyFileResult(r, g) {
  const fields = ['schemaVersion', 'workspaceId', 'requestId', 'purpose', 'sourceSha256', 'sourceSizeBytes', 'detectedContentType', 'contentValidated', 'records', 'imageBase64'];
  const invalid = () => { throw new WorkerError(502, 'FILE_WORKER_RESULT_MISMATCH'); };
  if (!r || typeof r !== 'object' || Object.keys(r).length !== fields.length || fields.some(k => !Object.hasOwn(r, k))) invalid();
  if (r.schemaVersion !== 3 || r.workspaceId !== g.workspaceId || r.requestId !== g.requestId || r.purpose !== g.purpose || r.sourceSha256 !== g.sha256 || r.sourceSizeBytes !== g.sizeBytes || r.detectedContentType !== g.contentType || r.contentValidated !== true) invalid();
  if (!Array.isArray(r.records) || r.records.length > 10001 || typeof r.imageBase64 !== 'string' || r.imageBase64.length > 1400000) invalid();
  let bytes = 0;
  for (const row of r.records) {
    if (!Array.isArray(row) || row.length > 200) invalid();
    for (const cell of row) {
      if (typeof cell !== 'string' || Buffer.byteLength(cell) > 65536 || /[\u0000\uD800-\uDFFF]/u.test(cell)) invalid();
      bytes += Buffer.byteLength(cell);
      if (bytes > 2 * 1024 * 1024) invalid();
    }
  }
  if (g.purpose !== 'tabular' && r.records.length) invalid();
  if (['signature-image', 'public-image'].includes(g.purpose)) {
    if (!r.imageBase64 || !/^[A-Za-z0-9+/]+={0,2}$/.test(r.imageBase64)) invalid();
    const image = Buffer.from(r.imageBase64, 'base64');
    if (image.length > 1024 * 1024 || image.toString('base64') !== r.imageBase64 || image.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') invalid();
  } else if (r.imageBase64) invalid();
  return r;
}
