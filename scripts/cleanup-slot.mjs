import { APIError, Sandbox } from '@vercel/sandbox';
import { sdkAuth } from '../lib/sandbox.mjs';

// Recovery only for an expired/stopped slot after interrupted Function cleanup.
// Never resume, inspect files, snapshot or delete a running job.
const name = process.env.OCR_SANDBOX_SLOT || 'ledgerical-ocr-active';
try {
  const sandbox = await Sandbox.get({ ...sdkAuth(), name, resume: false });
  if (!['stopped', 'failed'].includes(sandbox.status)) {
    console.error('Slot is still active. Wait for its 45-second VM deadline before cleanup.');
    process.exitCode = 1;
  } else {
    await sandbox.delete({ signal: AbortSignal.timeout(15000) });
    console.log('Expired slot removed. The next signed request will create a fresh VM.');
  }
} catch (error) {
  if (error instanceof APIError && error.response.status === 404) console.log('No stale slot exists.');
  else { console.error('Slot cleanup failed; check the project and your Vercel authentication.'); process.exitCode = 1; }
}
