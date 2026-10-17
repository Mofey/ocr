/**
 * Signed receipt-preview request for trusted Node.js environments.
 * Run after deploying the worker. This creates a real, billable OCR job.
 * The response is printed only to this local CLI's terminal.
 */
import { createReadStream } from 'node:fs';
import { basename, extname } from 'node:path';
import { createHash, createHmac, randomUUID } from 'node:crypto';
import { MAX_INPUT, MAX_OUTPUT, verifyResult } from '../lib/protocol.mjs';

/** @param {AsyncIterable<Uint8Array>} stream @param {number} maximum */
async function readBounded(stream, maximum) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stream) {
    size += chunk.byteLength;
    if (size > maximum) throw new Error(`Content exceeds the ${maximum}-byte limit.`);
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks, size);
}

async function main() {
  const filePath = process.argv[2];
  if (!filePath) throw new Error('Usage: node --env-file=.env.local examples/process-document.mjs <file>');
  const origin = process.env.OCR_WORKER_URL;
  const secret = process.env.OCR_WORKER_GRANT_SECRET ?? '';
  if (!origin || Buffer.byteLength(secret) < 32) {
    throw new Error('Set OCR_WORKER_URL and the existing OCR_WORKER_GRANT_SECRET in your local environment.');
  }
  const endpoint = new URL('/v1/process', origin);
  if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password) {
    throw new Error('Use the deployed worker\'s HTTPS origin without embedded credentials.');
  }
  const filename = basename(filePath);
  /** @type {Record<string, string>} */
  const formats = { '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' };
  const contentType = formats[extname(filename).toLowerCase()];
  if (!contentType || filename.length > 255) throw new Error('Choose a PDF, PNG, JPEG or WebP with a basename of at most 255 characters.');
  const body = await readBounded(createReadStream(filePath), MAX_INPUT);
  if (body.byteLength === 0) throw new Error('Choose a nonempty file.');
  const issuedAt = Math.floor(Date.now() / 1000);
  const grant = {
    schemaVersion: 2, audience: 'document-ocr-worker', purpose: 'receipt-preview',
    requestId: randomUUID(), workspaceId: randomUUID(), filename, contentType,
    sizeBytes: body.byteLength,
    sha256: createHash('sha256').update(body).digest('hex'),
    issuedAt, expiresAt: issuedAt + 60,
  };
  const payload = Buffer.from(JSON.stringify(grant)).toString('base64url');
  const signature = createHmac('sha256', secret).update(payload, 'ascii').digest('base64url');
  const response = await fetch(endpoint, {
    method: 'POST', body, redirect: 'error', signal: AbortSignal.timeout(45000),
    headers: {
      'Content-Type': 'application/octet-stream', 'Content-Length': String(body.byteLength),
      'X-Ledgerical-OCR-Job-Grant': `${payload}.${signature}`,
    },
  });
  if (!response.body) throw new Error(`The worker returned an empty response (HTTP ${response.status}).`);
  const encoded = await readBounded(response.body, MAX_OUTPUT);
  let result;
  try { result = JSON.parse(encoded.toString('utf8')); }
  catch { throw new Error(`The worker returned a non-JSON response (HTTP ${response.status}). Check the deployment URL and access settings.`); }
  if (!response.ok) {
    // Report bounded error codes only; never print a raw error response or grant.
    const code = result?.detail?.error;
    const safeCode = typeof code === 'string' && /^[A-Z0-9_]{1,80}$/.test(code) ? code : 'REQUEST_FAILED';
    throw new Error(`HTTP ${response.status}: ${safeCode}`);
  }
  console.log(JSON.stringify(verifyResult(result, grant), null, 2));
}

try { await main(); }
catch (error) {
  console.error(error instanceof Error ? error.message : 'The OCR example failed.');
  process.exitCode = 1;
}
