# Video Chunker

A small Cloud Run service that splits a video into ~10-second chunks with
clean, frame-accurate seams. Native `ffmpeg` does the work server-side, so a
split takes seconds instead of the minutes `ffmpeg.wasm` took in the browser.
The browser only uploads the source file and downloads the resulting zip.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | Flask app: `GET /`, `GET /healthz`, `POST /split` |
| `chunking.py` | Chunk-boundary rule (10s chunks; remainder ≤5s merges, ≥6s splits off) |
| `templates/index.html` | Upload page served at `/` |
| `Dockerfile` | `python:3.12-slim` + `ffmpeg`, served by gunicorn |
| `requirements.txt` | Flask, gunicorn |

## Endpoints

- `GET /` — upload page
- `GET /healthz` — health check (returns `ok`)
- `POST /split` — multipart form: `file`, `prefix`, `chunk_len`; returns a zip

## Access model

There is no app-level auth on purpose. Access is meant to be gated by Cloud Run
IAM — deploy **without** `--allow-unauthenticated` and grant
`roles/run.invoker` to the people (or a Google group) who should reach it. IAM
is the wall.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | Set by Cloud Run; gunicorn binds to it |
| `MAX_UPLOAD_BYTES` | `2147483648` (2 GB) | Rejects larger uploads |

## Local run

```bash
pip install -r requirements.txt
# needs ffmpeg + ffprobe on PATH
python app.py
# http://localhost:8080
```

Or via Docker:

```bash
docker build -t video-chunker .
docker run --rm -p 8080:8080 video-chunker
```

## Deploy to Cloud Run

Prerequisites: `gcloud` CLI installed and authenticated (`gcloud auth login`),
and a GCP project with billing enabled.

```bash
# One-time setup
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Deploy straight from source (Cloud Build builds the Dockerfile for you)
gcloud run deploy video-chunker \
  --source . \
  --region europe-west2 \
  --no-allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 600 \
  --concurrency 4 \
  --max-instances 3
```

Notes:

- `--memory`/`--cpu`: ffmpeg re-encodes in memory and writes chunks to the
  instance's local disk (tmpfs, counts against memory). 2 GB / 2 vCPU is a
  sane starting point; raise for large 4K sources.
- `--timeout 600`: matches the gunicorn timeout in the Dockerfile so long
  splits aren't cut off. 3600s is the Cloud Run max if you need it.
- `--concurrency 4`: one instance handles a few requests; ffmpeg is
  CPU-bound so don't push this high.

### Grant access (IAM)

```bash
# A single user
gcloud run services add-iam-policy-binding video-chunker \
  --region europe-west2 \
  --member "user:someone@example.com" \
  --role roles/run.invoker

# Or a whole Google Workspace group / domain
gcloud run services add-iam-policy-binding video-chunker \
  --region europe-west2 \
  --member "domain:example.com" \
  --role roles/run.invoker
```

Signed-in users then reach the service through an identity-aware proxy or by
sending a bearer token:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  https://video-chunker-XXXXXXXX-nw.a.run.app/healthz
```

### Continuous deploy from GitHub (optional)

Connect this repo in the Cloud Run console (**Set up continuous deployment**),
or with the CLI:

```bash
gcloud run deploy video-chunker \
  --region europe-west2 \
  --source . \
  --no-allow-unauthenticated
```

wired to a Cloud Build trigger on push to `main`. The build uses the
`Dockerfile` in the repo root.
