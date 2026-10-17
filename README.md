# Ledgerical OCR worker for Vercel

An independent Vercel project that runs **PaddleOCR**, connected to the existing
Main service. You can copy this entire folder into a separate repository. It
has its own source, dependency lockfiles, endpoints and deployment settings;
it does not import CMD, Main or the frontend.

**This folder is prepared code, not a deployed service.** Vercel Sandbox access,
a clean model snapshot and the environment settings below are required before
it can process receipts. No hosting resources have been created yet.

## How it works

```text
CMD signs the request → Main relays the file → Vercel Function verifies it
  → fresh offline Sandbox VM → restricted container → isolated PaddleOCR child
  → bounded JSON back to Main → discard container and VM
```

The Function authenticates before creating a paid sandbox. It accepts the same
`POST /v1/process` binary body and `X-Ledgerical-OCR-Job-Grant` header Main
already sends. Both stored Files grants (schema 1) and temporary receipt preview
grants (schema 2) are supported. No Main code change is needed to select this
worker; change `OCR_WORKER_URL` after deployment.

Local receipt previews still extract only when **Process selected file** is
clicked. They do not save the original in Files, R2, a database, a snapshot or
a persistent disk. **Save Receipt** saves reviewed fields through the existing
CMD flow. Existing Files sources remain stored by Files as before.

## 1. Create a Vercel project for this folder

Import your repository into a **new** Vercel project:

| Setting | Value |
| --- | --- |
| Root Directory | `ocr-worker-vercel` (or repository root if you copied this folder into its own repo) |
| Framework Preset | Other |
| Node.js version | 24.x |
| Install command | `npm ci` |
| Build command | `npm run build` |
| Output Directory | Leave unset |
| Fluid Compute | Enabled |

The project deploys small Node Functions. The Python packages and OCR weights
live in a separate Sandbox snapshot, so Vercel does not have to bundle Paddle
inside the HTTP Function. `sandbox/Dockerfile` builds the parser container
**inside** the Sandbox; the Vercel Function entry points are in `api/`.

Ensure Vercel Sandbox is available to your project/team. The setup script and
receipt jobs use billable compute; configure your Vercel spending controls.

## 2. Prepare the OCR models once

Run these commands from this folder on your computer, using Node 24:

```powershell
npm ci
npx vercel link
npx vercel env pull .env.local
npm run prepare:snapshot
```

Select the **new worker project** when linking. `vercel env pull` supplies a
local Vercel OIDC token; refresh it if it expires. Keep `.env.local` private.
The deployed Functions use Vercel's automatically managed OIDC authentication.
No Vercel access token is required in Main or CMD.

The preparation command creates an empty VM, uploads only explicitly listed
worker source files, installs Docker, builds the pinned Python 3.12 image and
downloads PaddleOCR's models. It then blocks outbound traffic and runs the
container/kernel startup guards. If the required restrictions are unavailable,
preparation fails; it does not bypass them. No user document is involved.

After stopping Docker and removing the empty RAM scratch mount, it snapshots
**code and models only**, deletes the preparation VM, and prints:

```dotenv
OCR_SANDBOX_SNAPSHOT_ID=snap_...
OCR_SANDBOX_REGION=iad1
```

The snapshot does not expire automatically. Delete superseded model snapshots
in Vercel after replacing them. Repeat preparation when changing the Python
code, Dockerfile, packages, models or sandbox shell scripts. Redeploying the
Function alone does not update an existing snapshot.

## 3. Configure and deploy the worker

Set these variables in the **worker project's Production environment**:

| Variable | Value |
| --- | --- |
| `OCR_WORKER_GRANT_SECRET` | Copy the exact existing value from CMD; at least 32 random UTF-8 bytes. |
| `OCR_SANDBOX_SNAPSHOT_ID` | The ID printed by preparation. |
| `OCR_SANDBOX_REGION` | The region printed by preparation; default `iad1`. |
| `OCR_SANDBOX_SLOT` | Optional; default `ledgerical-ocr-active`. Keep a single fixed slot per project. |

Then redeploy from Vercel or run:

```powershell
npx vercel --prod
```

Use a stable production domain, for example
`https://ledgerical-ocr.vercel.app`. Production must accept server-to-server
requests from Main without a Vercel login/password screen. Every processing
request is still authenticated with CMD's signed grant. This worker is not
called directly by the browser and does not enable browser CORS.

## 4. Connect Main

In **Main's** hosting settings, set:

```dotenv
OCR_WORKER_URL=https://YOUR-WORKER-PROJECT.vercel.app
```

Use the origin only, without `/v1/process` or `/healthz`. The old
`http://ocr-worker:8010` service has been removed. Main's Docker Compose now
reads `OCR_WORKER_URL` from Main's environment too; it runs only Main and MongoDB.
Restart/redeploy Main after updating its setting.

CMD keeps its current `MAIN_URL`, service credential and
`DOCUMENT_OCR_ENABLED=true`. CMD and this worker share the grant secret.
**Do not put that secret in Main.** Do not add database, R2, CMD/Main service,
cloud account or AI-provider credentials to a Sandbox or the parser container.

## Endpoints and limits

| Endpoint/control | Behavior |
| --- | --- |
| `GET /healthz` | Checks configuration only; returns `status: configured`, `inferenceVerified: false`. Creates no VM. |
| `POST /v1/process` | Main-compatible signed binary request; returns existing OCR JSON schema. |
| Input | At most 2 MiB; PDF/JPEG/PNG/WebP; exactly one PDF page or at most 4 million image pixels. |
| Grant | HMAC-SHA256, audience/purpose/scope/size/type/hash checked, lifetime at most 120 seconds. |
| Concurrency | One named sandbox slot per project. A concurrent request gets HTTP 429. |
| Parser | At most 25 seconds, one CPU, 1 GiB RAM, 128 processes, 64 MiB memory-only `/tmp`. |
| Gateway | 32-second processing deadline plus bounded cleanup, inside Main's existing 36-second worker wait. |
| VM | One vCPU/2 GiB, hard 45-second lifetime, no exposed ports, outbound traffic denied from creation. |
| Output | At most 1 MiB, 200,000 text characters, 500 layout lines; schema and request bindings checked again in Main. |

## Protections

- Every job creates a **fresh**, `persistent: false` Sandbox from the clean
  model snapshot. It never resumes a receipt VM, attaches a storage drive or
  snapshots a job.
- The grant key and Vercel OIDC credential stay in the trusted Function. Neither
  is transferred to the VM, container, grant claims file or parser child.
- A non-root container has a read-only root, no capabilities, no new privileges,
  Docker's seccomp profile, no network, no swap and bounded cgroups. The
  worker's runtime guards and parser syscall filter remain enabled.
- The child cannot initiate connections, execute programs, create descendant
  processes, inspect/signal other processes or change namespaces. Failure to
  install the filter aborts parsing.
- Upload and result files exist only in a dedicated 16 MiB RAM mount inside the
  VM. Parser scratch/locks/caches stay in the container's separate bounded RAM
  mount; model weights remain read-only. VM swap is disabled before upload.
- Document bytes and extracted content are never printed into Function, Sandbox
  command or Docker logs. Output returns through a bounded file read, not stdout.
- Success, rejection, parser crash and timeout all enter VM stop/delete cleanup.
  A platform interruption still has a hard VM expiration; interrupted cleanup
  can leave the single slot blocked until operator recovery.

These controls contain untrusted parsing; they do not detect malware or provide
a guarantee against every vulnerability. Runtime enforcement, model startup,
real OCR results, cold-start timing and cleanup still need evidence on Vercel.
An HTTP health response alone proves none of those. No real receipt or browser
journey was run while preparing this project.

## Troubleshooting

| Code/symptom | Next step |
| --- | --- |
| `OCR_WORKER_NOT_CONFIGURED` | Set the secret and snapshot ID on the worker, then redeploy it. |
| `INVALID_OCR_JOB_GRANT` | Check that CMD and worker use the exact same secret and system clocks are correct. |
| `OCR_SANDBOX_UNAVAILABLE` | Re-run preparation and inspect infrastructure restrictions; keep the guards enabled. |
| `OCR_WORKER_TIMEOUT` | Measure snapshot restore/model startup in staging. No automatic fallback to parsing in Main exists. |
| `OCR_WORKER_BUSY` / repeated 429 | A job is active, or a stale slot remains after interrupted cleanup. |
| `OCR_SANDBOX_CLEANUP_INCOMPLETE` | Wait for the VM's 45-second deadline, then remove the stopped slot. |
| Main still uses 503 | Confirm Main points to this deployed origin, the endpoint is reachable without a Vercel login, and the clean snapshot exists in the configured region. |

To remove a **stopped/failed** stale slot, refresh local authentication and run:

```powershell
npx vercel env pull .env.local
npm run cleanup:slot
```

Pause new receipt requests while running one recovery command. It refuses to
delete an active job and never resumes or snapshots a VM. If you changed the
slot name, set that same `OCR_SANDBOX_SLOT` in `.env.local` before recovery.

## Source maintenance and static checks

`sandbox/ocr_worker/` is the maintained Python worker source. Its nine core
source/dependency files are recorded with SHA-256 in `vendor-manifest.json`.
Review and update the manifest when editing those files. `.gitattributes`
preserves their Linux line endings. `vercel_runner.py` is the one-shot Vercel
adapter. The duplicate worker and its Docker/ingress deployment templates have
been removed from Main. This folder does not need the parent repository to
build or deploy.

`npm run build` checks JavaScript syntax, configuration JSON and worker source hashes.
It does not create cloud resources, run inference or execute tests.

The five existing worker-only Python test files have been moved from Main into
`tests/` without changing their contents. `pytest.ini` resolves imports from
`sandbox/`; `requirements-test.txt` provides their separate Python 3.12
dependencies. They are excluded from Vercel deployments and are not executed
by the build. Main's test image no longer imports or installs PaddleOCR.

Implementation references:
[Vercel Sandbox SDK](https://vercel.com/docs/sandbox/sdk-reference),
[Docker inside Sandbox](https://vercel.com/changelog/run-docker-containers-inside-vercel-sandbox),
[outbound firewall](https://vercel.com/docs/sandbox/concepts/firewall),
[nonpersistent sandboxes](https://vercel.com/docs/sandbox/concepts/persistent-sandboxes),
[snapshot lifecycle](https://vercel.com/docs/sandbox/concepts/snapshots),
[OIDC authentication](https://vercel.com/docs/sandbox/concepts/authentication).
