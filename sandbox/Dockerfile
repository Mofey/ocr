FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PADDLE_PDX_CACHE_HOME=/opt/paddle-cache \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True HF_HUB_OFFLINE=1 \
    TMPDIR=/tmp HOME=/tmp/worker-home \
    OCR_WORKER_JOB_TIMEOUT_SECONDS=25 \
    OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /worker
# Public trust certificates only, supplied by the trusted preparation script.
# Nested Docker containers do not inherit the sandbox host's proxy CA.
COPY build-certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /opt/paddle-cache /tmp/worker-home /input /output \
    && chown -R 10001:10001 /opt/paddle-cache /tmp/worker-home

COPY paddle-dependencies.in paddle-dependencies-linux.lock ./
RUN pip install --no-cache-dir --cert /etc/ssl/certs/ca-certificates.crt -r paddle-dependencies-linux.lock
COPY ocr_worker/ ./ocr_worker/
# Models are fetched ONCE, during trusted preparation, before any user input.
RUN HF_HUB_OFFLINE=0 python -c 'from ocr_worker.ocr import _get_ocr_reader; _get_ocr_reader()' \
    && rm -rf /opt/paddle-cache/locks /opt/paddle-cache/temp /opt/paddle-cache/func_ret \
    && ln -s /tmp/paddle-locks /opt/paddle-cache/locks \
    && ln -s /tmp/paddle-temp /opt/paddle-cache/temp \
    && ln -s /tmp/paddle-functions /opt/paddle-cache/func_ret \
    && chown -R 10001:10001 /worker /opt/paddle-cache \
    && chmod -R a+rX,go-w /worker /opt/paddle-cache

ENV OCR_PREPARED_MODELS_ONLY=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "ocr_worker.vercel_runner"]
