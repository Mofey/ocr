import { configuration, jsonResponse, WorkerError } from '../lib/protocol.mjs';

export default {
  /** @param {Request} request */
  fetch(request) {
    if (request.method !== 'GET') return jsonResponse(405, { detail: { error: 'METHOD_NOT_ALLOWED' } });
    try {
      configuration();
      // No paid sandbox creation or inference on a public health request.
      return jsonResponse(200, { status: 'configured', worker: 'vercel-sandbox-ocr-v1', inferenceVerified: false });
    } catch (error) {
      return jsonResponse(503, { detail: { error: error instanceof WorkerError ? error.code : 'OCR_WORKER_NOT_CONFIGURED' } });
    }
  },
};
