# Video Chunker

A small Cloud Run service that splits a video into ~10-second chunks with
clean, frame-accurate seams. Native `ffmpeg` does the work server-side, so a
split takes seconds instead of the minutes `ffmpeg.wasm` took in the browser.

For anything but tiny clips the browser uploads the source **straight to Cloud
Storage** via a short-lived signed URL, then calls the service with just the
object name — this dodges Cloud Run's 32 MiB request-body limit. The result zip
is streamed back with chunked transfer encoding, which is exempt from the
matching 32 MiB response limit, so it can be any size.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | Flask app: `/`, `/config`, `/healthz`, `/signed-upload`, `/split` |
| `chunking.py` | Chunk-boundary rule (10s chunks; remainder ≤5s merges, ≥6s splits off) |
| `hashes.py` | Recent-upload fingerprint store (`hashes.json` in the bucket) for the duplicate warning |
| `templates/index.html` | Upload page served at `/` |
| `Dockerfile` | `python:3.12-slim` + `ffmpeg`, served by gunicorn |
| `requirements.txt` | Flask, gunicorn, google-cloud-storage |

## Endpoints

- `GET /` — upload page
- `GET /config` — `{gcs: bool, max_upload_bytes: int}`; the page uses this to pick its upload path
- `GET /healthz` — health check (returns `ok`)
- `POST /signed-upload` — JSON `{filename}` → `{url, object, method, headers, expires_in}`; a v4 signed `PUT` URL for the bucket. `501` if no bucket is configured.
- `POST /split` — either JSON `{object, prefix, chunk_len}` (GCS flow) or multipart `file`, `prefix`, `chunk_len` (fallback). Streams back a zip. Also accepts `production` (default `1`) and `force` (default `0`) — see below.

## Production-video duplicate warning

When **Production video** is checked (the default), `/split` fingerprints the
source with a streamed SHA-256 and compares it against `hashes.json` in
`GCS_BUCKET` — an ordered list of the last 30 digests, newest first, no dates or
user info. An exact byte-for-byte repeat returns **HTTP 409**
`{"duplicate": true, "message": …}` instead of splitting; the page then offers
**Split anyway** (resends with `force=1`, which records the digest again and
proceeds) or **Cancel**. A non-duplicate records its digest and splits in one
shot. Unchecking **Production video** skips all of it — nothing is read,
recorded, or flagged.

This is entirely best-effort: every read and write of `hashes.json` swallows
errors, so if the bucket or object is unreachable the check is silently skipped
and the split proceeds. The feature can never block or fail a split. No new env
var — it reuses `GCS_BUCKET` and the app's storage client. (With no bucket
configured, e.g. local runs, the check is simply inert.)

## Access model

There is no app-level auth on purpose. Access is gated by Cloud Run IAM / IAP —
deploy **without** `--allow-unauthenticated`. See "Grant access" below. IAM is
the wall.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | Set by Cloud Run; gunicorn binds to it |
| `MAX_UPLOAD_BYTES` | `2147483648` (2 GB) | Cap on the **fallback** multipart upload only |
| `GCS_BUCKET` | _(unset)_ | Bucket for direct uploads. Unset → fallback multipart path (32 MiB cap on Cloud Run) |
| `SIGNED_URL_TTL_MIN` | `15` | Lifetime of the signed upload URL, in minutes |
| `GOOGLE_APPLICATION_CREDENTIALS` | _(unset)_ | Local only: path to a service-account key that can sign URLs directly |

## Local run

```bash
pip install -r requirements.txt
# needs ffmpeg + ffprobe on PATH
python app.py
# http://localhost:8080  — runs the fallback multipart path with no bucket
```

To exercise the GCS path locally, set `GCS_BUCKET` and point
`GOOGLE_APPLICATION_CREDENTIALS` at a key file for a service account with object
write access to that bucket.

Or via Docker:

```bash
docker build -t video-chunker .
docker run --rm -p 8080:8080 video-chunker
```

## Deploy to Cloud Run

Prerequisites: `gcloud` CLI installed and authenticated (`gcloud auth login`),
and a GCP project with billing enabled. Examples use region
`northamerica-northeast2` — change to taste, but keep the bucket in the **same
region** as the service so traffic between them stays free.

```bash
# One-time setup
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com iamcredentials.googleapis.com
```

### 1. Bucket for uploads

```bash
REGION=northamerica-northeast2
BUCKET=YOUR_PROJECT_ID-video-chunker

gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access

# Auto-delete uploads and generated objects after 1 day so storage never grows.
cat > /tmp/lifecycle.json <<'EOF'
{ "rule": [ { "action": {"type": "Delete"}, "condition": {"age": 1} } ] }
EOF
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lifecycle.json

# CORS so the browser can PUT directly to the bucket. Replace the origin with
# the service URL (and any custom domain) once you have it.
cat > /tmp/cors.json <<'EOF'
[ { "origin": ["https://YOUR_SERVICE_URL"], "method": ["PUT"],
    "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600 } ]
EOF
gcloud storage buckets update gs://$BUCKET --cors-file=/tmp/cors.json
```

### 2. Deploy

Deploy **from the repo** (`--source .`), not the console — committed code and the
running service have drifted before.

```bash
gcloud run deploy video-chunker \
  --source . \
  --region $REGION \
  --no-allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 600 \
  --concurrency 4 \
  --min-instances 0 \
  --max-instances 3 \
  --set-env-vars GCS_BUCKET=$BUCKET
```

Notes:

- `--memory`/`--cpu`: ffmpeg re-encodes and writes chunks to the instance's
  local disk (tmpfs, counts against memory). 2 GB / 2 vCPU is a sane start;
  raise for large 4K sources.
- `--timeout 600`: matches the gunicorn timeout in the Dockerfile. 3600s is the
  Cloud Run max.
- `--concurrency 4`: ffmpeg is CPU-bound, so don't push this high.
- `--min-instances 0`: keep scaling request-based / scale-to-zero. A
  manual-scaling or min-1 setting has previously pinned an instance on 24/7 and
  run up the bill. After deploying, confirm in the console that the revision
  shows **min instances 0** and CPU is **only allocated during requests**.

### 3. Grant the runtime service account access

Find the service account the service runs as
(`gcloud run services describe video-chunker --region $REGION --format='value(spec.template.spec.serviceAccountName)'`
— empty means the Compute Engine default, `PROJECT_NUMBER-compute@developer.gserviceaccount.com`).

```bash
SA=PROJECT_NUMBER-compute@developer.gserviceaccount.com

# Read/write/delete objects in the bucket
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin

# Sign URLs with no key file: let the SA mint signatures for itself
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="serviceAccount:$SA" --role=roles/iam.serviceAccountTokenCreator
```

### 4. Set the CORS origin

Re-run the CORS step from part 1 with the real service URL
(`gcloud run services describe video-chunker --region $REGION --format='value(status.url)'`),
plus any custom domain you map.

## Grant access (who can use it)

Deploy keeps the service private. Choose one:

**IAP directly on the service** (browser access at the `run.app` URL, no proxy):

```bash
gcloud run services update video-chunker --region $REGION --iap
```

then grant users the IAP role (a Workspace domain works if the project is in
that org):

```bash
gcloud run services add-iam-policy-binding video-chunker \
  --region $REGION \
  --member="domain:example.com" \
  --role=roles/iap.httpsResourceAccessor
```

Console equivalent: service → **Security** → Require authentication → Identity-Aware
Proxy; then add the principal on the **IAP** page with role **IAP-secured Web App User**.

**Or plain invoker + proxy** (no IAP): grant `roles/run.invoker` to the
users/group/domain, then `gcloud run services proxy video-chunker --region $REGION`
and open `http://localhost:8080`.

## Continuous deploy from GitHub

Connect this repo in the Cloud Run console (**Create Service → Continuously
deploy from a repository**), branch `main`, build type **Dockerfile**. That
provisions a Cloud Build trigger that rebuilds and redeploys on every push to
`main`. Environment variables set on the service (like `GCS_BUCKET`) persist
across those deploys.
