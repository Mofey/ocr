# Isolated file processing

The worker supports OCR and general file processing. No malware scanner is used.
Validation/extraction is not a certification that an original file is safe to open.

## Trust boundaries

1. CMD authenticates the actor, resolves their workspace and checks permissions.
2. Browser uploads go to a private, unique quarantine key in R2. CMD streams
   opaque bytes to verify size, SHA-256, upload identity and object consistency.
3. CMD promotes the immutable version with `contentValidation.status=pending`.
   A separate durable validation job keeps downloads and OCR blocked until ready.
4. The trusted Vercel Function authenticates a short-lived signed job, fetches
   only its authorized object from the configured R2 origin, bounds and hashes
   the bytes, then creates a fresh nonpersistent, network-denied Sandbox.
5. Only file bytes and bounded job identifiers enter the parser container.
   Storage URLs, signing keys, application credentials and provider credentials
   stay outside it. Existing container resource/syscall restrictions apply.
6. The Function validates purpose, workspace, request, digest, size, MIME and
   bounded result fields. CMD repeats those checks before accepting results.
7. The VM is deleted on success, error or cancellation. Failed cleanup fails the
   request. No customer input or output is captured in the prepared snapshot.

`POST /v1/process` preserves the existing OCR v1/v2 contracts used by Main.
`POST /v1/files/process` accepts only CMD-signed v3 grants for `validate`,
`tabular`, `signature-image` and `public-image`. PaddleOCR is used only for OCR.

Server-side image/PDF/Office/CSV decoding runs in the parser environment. Excel imports use
bounded actual ZIP expansion and XML parsing there; no macros, formulas or
external relationships are executed. CMD consumes bounded JSON records and
performs ordinary financial/business validation. It never falls back to local
file parsing when the worker fails.

## Deploy in this order

1. Push the updated standalone `ocr` repository. Keep the Vercel Root Directory
   empty. Keep the existing Node build settings from `vercel.json`.
2. Prepare a **new** snapshot from this source using `npm run prepare:snapshot`.
   Preparation creates billable Sandbox resources and contains no customer files.
   An old snapshot does not contain `file_processor.py` or the v3 runner.
3. Set Vercel Production variables:
   - `OCR_SANDBOX_SNAPSHOT_ID`: the new value printed by preparation.
   - `OCR_WORKER_GRANT_SECRET`: the existing CMD signing secret, at least 32 bytes.
   - `FILE_PROCESSOR_SOURCE_ORIGIN`: the exact HTTPS origin of CMD's R2 endpoint,
     e.g. `https://<account-id>.r2.cloudflarestorage.com`. No bucket/path/query.
   - Keep the existing sandbox region and fixed slot settings.
4. Redeploy the Vercel gateway. `/healthz` reports configuration only, not proof
   that inference or a fresh VM works.
5. Set **CMD** `OCR_WORKER_URL` to the worker base HTTPS origin, for example
   `https://ocr-nu-seven.vercel.app`. Keep its existing matching grant secret.
   Keep `CRON_ENABLED=true`; file validation needs the durable job dispatcher
   even when optional OCR is disabled. Main retains its existing worker URL.
6. Deploy CMD; let migration **0309** complete before serving the updated APIs.
   It creates validation jobs, processed public images and upload history,
   preserves historical attribution as unverified, and protects uploader/history
   records with PostgreSQL triggers. It does not invent prior consent.
7. Deploy the frontend together with CMD. Uploaded documents wait for validation
   before an import/attachment consumer uses them. Receipt-local preview remains
   transient and starts only after the existing processing button is clicked.

Vercel's request limit prevents sending 10 MB directly to a Function. Larger
inputs use short-lived signed private R2 reads. The parser VM cannot use those
URLs. Transient table previews use a temporary private object with a durable
deletion outbox when necessary; no Files record is created.

## Account access and attribution

- Originals always download through authenticated, permission-checked CMD
  streams. No storage GET credential is returned to the browser. Existing
  short-lived URLs issued before deployment must expire before acceptance.
- Public/vendor-token download paths and new private-file shares are disabled.
  An external vendor cannot list another account's private documents.
- Public branding/profile images contain only isolated canonical PNG
  derivatives. Publishing a brand does not expose its private original.
- Each upload records the authenticated member ID and a name snapshot, account,
  session, version, digest, size, timestamp and request reference. Started and
  completed stages are separate immutable records. Token recipients and old
  unverified uploads are explicitly distinguished from authenticated members.
- `GET /api/files/upload-history?documentId=<uuid>` returns account-scoped history.
  Owners may investigate purged files; team members require current read access.
  Records survive file/member deletion and are protected for one year. Ordinary
  application requests cannot edit them. Legal/privacy retention changes require
  a deliberate policy/migration decision.
- The UI records notice version `file-notice-2026-10-18` for confirmed transfers.
  The qualified notice links to `/terms#uploaded-files`; it does not certify that
  files are clean or replace Ledgerical's security responsibilities.
- If a file check failed because the service was unavailable, **Retry file
  check** brings it forward after service restoration. Invalid files stay
  rejected. Three manual retry cycles and five automatic attempts per cycle
  bound repeated work; retrying a check never starts OCR.

## Staging evidence still required

No production deployment, live customer document processing, browser journeys or
malicious sample execution is implied by a successful build. Record deployment
IDs, migration version, request/job IDs and outcomes for the following cases:

| Area | Required outcome |
| --- | --- |
| Two independent accounts | Account B cannot list, fetch, preview, download, process, restore, attach or retrieve results/history for A's IDs, including mixed document/version IDs. |
| Copied URLs | Private download endpoints still require B's own authorization; no reusable storage URL appears in the response. |
| Old portal shares | Token download routes return no private bytes or document listings. |
| Team attribution | Different members produce different stable uploader IDs; renaming a file/member and removing a member preserve the original history. |
| Historical files | Unverified attribution stays labelled as such; no false notice acceptance is backfilled. |
| Parser limits | Invalid image/PDF/Office/CSV inputs and excessive actual expansion fail within memory/time/output bounds without parser activity in CMD/Main. |
| Job isolation | No previous input/output, application secret, network access or writable model snapshot is available to a later parser job. |
| Forged/mismatched results | Wrong account/request/digest/size/purpose and malformed or oversized output are rejected. |
| Outage/restart | Pending validation stays unavailable; retries, stale claims and cleanup recover without local parsing or duplicate versions. |
| Receipts | Local selection saves no Files object and starts only on the processing button; existing Files selection processes only the selected version. |
| UI notice | Upload/download confirmation shows the short Terms notice; cancel sends no transfer; changing account/sign-in cancels the pending action. |

Use isolated staging accounts and synthetic non-sensitive inputs. These checks
are an acceptance plan, not a record that they have already passed.
