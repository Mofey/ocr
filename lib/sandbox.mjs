import { APIError, Sandbox } from '@vercel/sandbox';
import { Readable } from 'node:stream';
import { MAX_OUTPUT, WorkerError, verifyResult } from './protocol.mjs';
import { bootstrapFailureDetails } from './bootstrap-diagnostics.mjs';

// Keep processing + cleanup below Main's 36-second worker read timeout.
export const PROCESSING_BUDGET_MS = 28000;
const CLEANUP_BUDGET_MS = 7000;

// SDK calls run in the trusted Function. No project environment or credentials
// are copied into the VM, Docker container, input file or parser process.
export function sdkAuth() {
  if (!process.env.VERCEL_TOKEN) return {};
  if (!process.env.VERCEL_TEAM_ID || !process.env.VERCEL_PROJECT_ID) throw new WorkerError(503, 'OCR_SANDBOX_AUTH_NOT_CONFIGURED');
  return { token: process.env.VERCEL_TOKEN, teamId: process.env.VERCEL_TEAM_ID, projectId: process.env.VERCEL_PROJECT_ID };
}

/** @param {Sandbox} sandbox @param {AbortSignal} signal */
async function boundedFile(sandbox, signal) {
  const stream = await sandbox.readFile({ path: '/run/ledgerical-ocr/output/result.json' }, { signal });
  if (!stream) throw new WorkerError(503, 'OCR_WORKER_UNAVAILABLE');
  const chunks = [];
  let size = 0;
  // readFile returns a Node Readable in the SDK. Limit while streaming rather
  // than buffering an untrusted output file before checking its length.
  try {
    for await (const chunk of stream) {
      signal.throwIfAborted();
      const bytes = Buffer.from(chunk);
      size += bytes.byteLength;
      if (size > MAX_OUTPUT) throw new WorkerError(502, 'OCR_WORKER_RESPONSE_INVALID');
      chunks.push(bytes);
    }
  } finally {
    if (stream instanceof Readable) stream.destroy();
  }
  try { return JSON.parse(Buffer.concat(chunks, size).toString('utf8')); }
  catch { throw new WorkerError(502, 'OCR_WORKER_RESPONSE_INVALID'); }
}

/** @param {unknown} error */
function isConflict(error) {
  return error instanceof APIError && error.response.status === 409;
}

/**
 * @param {Buffer} body
 * @param {ReturnType<import('./protocol.mjs').verifyGrant>} grant
 * @param {ReturnType<import('./protocol.mjs').configuration>} config
 * @param {AbortSignal} signal
 */
export async function processInSandbox(body, grant, config, signal) {
  let sandbox;
  let result;
  let cleanupFailed = false;
  try {
    try {
      sandbox = await Sandbox.create({
        ...sdkAuth(), name: config.slot,
        source: { type: 'snapshot', snapshotId: config.snapshotId },
        resources: { vcpus: 1 }, region: config.region,
        persistent: false, networkPolicy: 'deny-all', ports: [], env: {},
        timeout: 45000, signal,
      });
    } catch (error) {
      if (isConflict(error)) throw new WorkerError(429, 'OCR_WORKER_BUSY');
      throw error;
    }
    if (sandbox.persistent !== false) {
      console.error('OCR_SANDBOX_CONFIGURATION_REJECTED', { persistenceDisabled: false });
      throw new WorkerError(503, 'OCR_SANDBOX_UNAVAILABLE');
    }
    // Boot and verify the same container/kernel restrictions as the existing
    // worker BEFORE transferring any document bytes into the microVM.
    const boot = await sandbox.runCommand({ cmd: '/opt/ledgerical/boot.sh', sudo: true, signal });
    if (boot.exitCode !== 0) {
      let stderr = '';
      try { stderr = await boot.stderr(); } catch { /* only emit safe fallback facts */ }
      console.error('OCR_SANDBOX_BOOT_FAILED', bootstrapFailureDetails(stderr, boot.exitCode));
      throw new WorkerError(503, 'OCR_SANDBOX_UNAVAILABLE');
    }
    signal.throwIfAborted();
    // This path is a freshly mounted, bounded tmpfs, not a persistent VM disk.
    // No document-bearing stdin, stdout, stderr or command-line arguments.
    await sandbox.writeFiles([{
      path: '/run/ledgerical-ocr/input/job.json', mode: 0o444,
      content: Buffer.from(JSON.stringify({ grant, contentBase64: body.toString('base64') })),
    }], { signal });
    const run = await sandbox.runCommand({ cmd: '/opt/ledgerical/process.sh', sudo: true, signal });
    if (run.exitCode !== 0) throw new WorkerError(503, 'OCR_WORKER_UNAVAILABLE');
    const envelope = await boundedFile(sandbox, signal);
    if (envelope.status !== 200) {
      /** @type {Record<string, number>} */
      const errors = {
        INVALID_DOCUMENT_FORMAT: 422, UNSUPPORTED_DOCUMENT_FORMAT: 415,
        SOURCE_HASH_MISMATCH: 409, OCR_INPUT_SIZE_MISMATCH: 409,
        OCR_WORKER_TIMEOUT: 504, OCR_WORKER_UNAVAILABLE: 503,
      };
      const code = envelope.error;
      if (!Object.hasOwn(errors, code ?? '') || errors[code] !== envelope.status) throw new WorkerError(502, 'OCR_WORKER_RESPONSE_INVALID');
      throw new WorkerError(envelope.status, code);
    }
    result = verifyResult(envelope.result, grant);
  } finally {
    if (sandbox) {
      // DELETE stops running sessions before deleting the sandbox. A separate
      // stop call adds latency and can time out even when deletion succeeds.
      // Use a fresh deadline: the request/processing signal may be aborted.
      // Never retry deletion by name: after an uncertain response, another
      // request may already have created a new VM in the same slot.
      try { await sandbox.delete({ signal: AbortSignal.timeout(CLEANUP_BUDGET_MS) }); }
      catch (error) {
        // The provider documents 404 when a concurrent cleanup already deleted
        // this sandbox. Its absence satisfies cleanup; other failures do not.
        cleanupFailed = !(error instanceof APIError && error.response.status === 404);
        if (cleanupFailed) {
          const status = error instanceof APIError ? error.response.status : undefined;
          const errorType = error instanceof Error && ['AbortError', 'TimeoutError'].includes(error.name)
            ? error.name : 'ProviderError';
          console.error('OCR_SANDBOX_DELETE_FAILED', { errorType, ...(status === undefined ? {} : { status }) });
        }
      }
      if (cleanupFailed) {
        // Static code only. SDK exception text can contain provider details.
        console.error('OCR_SANDBOX_CLEANUP_INCOMPLETE');
      }
    }
  }
  if (cleanupFailed) throw new WorkerError(503, 'OCR_SANDBOX_CLEANUP_INCOMPLETE');
  return result;
}
