import { configuration, jsonResponse, WorkerError } from '../lib/protocol.mjs';
import { readFileInput, verifyFileGrant } from '../lib/file-processing.mjs';
import { processInSandbox } from '../lib/sandbox.mjs';

export default {
  async fetch(request) {
    if (request.method !== 'POST') return jsonResponse(405, { detail: { error: 'METHOD_NOT_ALLOWED' } });
    // Leave cleanup time within Heroku's 30-second router deadline.
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(20000)]);
    let body;
    let requestId;
    try {
      const config = configuration();
      const grant = verifyFileGrant(request.headers.get('X-Ledgerical-File-Grant'), config.secret);
      requestId = grant.requestId;
      body = await readFileInput(request, grant, signal);
      return jsonResponse(200, await processInSandbox(body, grant, config, signal));
    } catch (error) {
      const failure = error instanceof WorkerError ? error : new WorkerError(signal.aborted ? 504 : 503, signal.aborted ? 'OCR_WORKER_TIMEOUT' : 'OCR_WORKER_UNAVAILABLE');
      if (failure.status >= 500) console.error(failure.code, requestId ? { requestId } : {});
      return jsonResponse(failure.status, { detail: { error: failure.code } });
    } finally { body?.fill(0); }
  },
};
