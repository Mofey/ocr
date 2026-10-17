import { configuration, jsonResponse, readInput, verifyGrant, WorkerError } from '../lib/protocol.mjs';
import { processInSandbox } from '../lib/sandbox.mjs';

export default {
  /** @param {Request} request */
  async fetch(request) {
    if (request.method !== 'POST') return jsonResponse(405, { detail: { error: 'METHOD_NOT_ALLOWED' } });
    // Main waits 36 seconds; reserve three seconds for stopping/deleting the VM.
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(32000)]);
    let body;
    try {
      const config = configuration();
      const grant = verifyGrant(request.headers.get('X-Ledgerical-OCR-Job-Grant'), config.secret);
      // Authentication and bounded body/hash validation precede any paid work.
      try {
        body = await readInput(request, grant, AbortSignal.any([signal, AbortSignal.timeout(5000)]));
      } catch (error) {
        if (error instanceof Error && error.name === 'TimeoutError') throw new WorkerError(408, 'OCR_INPUT_TIMEOUT');
        throw error;
      }
      const result = await processInSandbox(body, grant, config, signal);
      return jsonResponse(200, result);
    } catch (error) {
      const failure = error instanceof WorkerError ? error
        : new WorkerError(signal.aborted ? 504 : 503, signal.aborted ? 'OCR_WORKER_TIMEOUT' : 'OCR_WORKER_UNAVAILABLE');
      if (failure.status >= 500) console.error(failure.code);
      return jsonResponse(failure.status, { detail: { error: failure.code } });
    } finally {
      body?.fill(0);
    }
  },
};
