"""
Video Chunker — Cloud Run service.

Native ffmpeg does the splitting server-side, so it's fast (seconds, not the
minutes ffmpeg.wasm took in the browser) and the seams are frame-perfect.

Uploads: Cloud Run caps an HTTP/1 request body at 32 MiB, which real videos blow
past. So when a bucket is configured (GCS_BUCKET), the browser uploads the source
straight to Cloud Storage via a short-lived v4 signed URL and then calls /split
with just the object name. With no bucket configured the app falls back to a
plain multipart POST (fine locally and for clips under 32 MiB).

Downloads: the result zip is streamed back with chunked transfer encoding, which
is exempt from Cloud Run's 32 MiB response cap, so the zip can be any size.

Endpoints:
  GET  /              -> the upload page (index.html)
  GET  /config        -> {gcs: bool, max_upload_bytes: int} for the client
  GET  /healthz       -> health check for Cloud Run
  POST /signed-upload -> {filename} -> {url, object, ...} v4 signed PUT URL
  POST /split         -> JSON {object, prefix, chunk_len}  (GCS flow), or
                         multipart (file, prefix, chunk_len) (fallback); streams a zip.
                         A "production" flag (default on) fingerprints the source
                         (SHA-256) and returns 409 {duplicate:true} if it matches
                         a recently split file; resend with force=1 to split anyway.

Access is gated by Cloud Run IAM / IAP (see the README), so only signed-in
members of the Workspace domain can reach it. There is no app-level auth here on
purpose — IAM is the wall, the same way the Apps Script deploy setting was.
"""

import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta

from flask import Flask, request, Response, render_template, jsonify

import hashes
from chunking import compute_chunks

app = Flask(__name__)

# Cap the fallback multipart upload so a runaway file can't exhaust the instance.
# The GCS flow doesn't touch this (the bytes never pass through the app).
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024))

# When set, the browser uploads straight to this bucket via a signed URL.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
SIGN_TTL = timedelta(minutes=int(os.environ.get("SIGNED_URL_TTL_MIN", "15")))
# The Content-Type the PUT is signed with; the browser must send exactly this.
_UPLOAD_CT = "application/octet-stream"

# A safe filename prefix: letters, numbers, dash, underscore only.
_SAFE_PREFIX = re.compile(r"[^A-Za-z0-9_-]")


def safe_prefix(raw: str) -> str:
    p = _SAFE_PREFIX.sub("", (raw or "").strip()) or "clip"
    return p[:60]


def parse_chunk_len(raw) -> float:
    try:
        v = float(raw if raw not in (None, "") else 10)
        return v if v > 0 else 10.0
    except (TypeError, ValueError):
        return 10.0


def parse_flag(raw, default: bool) -> bool:
    """Read a checkbox-ish value from JSON (bool / 1 / 0) or a form (string)."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# --- Cloud Storage helpers -------------------------------------------------

_gcs_client = None


def _bucket():
    global _gcs_client
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET is not set")
    if _gcs_client is None:
        from google.cloud import storage  # lazy: not needed for the fallback path
        _gcs_client = storage.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def _metadata(path: str) -> str:
    import urllib.request
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{path}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.read().decode()


def signed_put_url(blob) -> str:
    """A v4 signed PUT URL for `blob`.

    Locally, GOOGLE_APPLICATION_CREDENTIALS points at a key file that can sign
    directly. On Cloud Run there is no key, so we sign through the IAM signBlob
    API using the runtime service account's own access token — which needs
    roles/iam.serviceAccountTokenCreator on that service account (see README).
    """
    common = dict(version="v4", expiration=SIGN_TTL, method="PUT", content_type=_UPLOAD_CT)
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return blob.generate_signed_url(**common)

    import google.auth
    import google.auth.transport.requests as greq

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(greq.Request())
    email = getattr(creds, "service_account_email", "") or ""
    if not email or email == "default":
        email = _metadata("instance/service-accounts/default/email")
    return blob.generate_signed_url(service_account_email=email, access_token=creds.token, **common)


# --- ffmpeg --------------------------------------------------------------------


def ffprobe_duration(path: str) -> float:
    """Return the video duration in seconds via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def split_video(src_path: str, prefix: str, chunk_len: float, out_dir: str):
    """Split src into chunks in out_dir using the tested boundary rule and the
    clean re-encode flags. Returns a list of (filename, path) in order."""
    duration = ffprobe_duration(src_path)
    chunks = compute_chunks(duration, chunk_len, 5.0)
    ext = os.path.splitext(src_path)[1].lstrip(".").lower() or "mp4"

    produced = []
    for c in chunks:
        out_name = f"{prefix}-{c.index}.{ext}"
        out_path = os.path.join(out_dir, out_name)
        # Clean-seam re-encode (verified frame-accurate natively):
        #   - accurate seek with -ss before -i
        #   - setpts / asetpts reset both streams to start exactly at 0
        #   - re-encode video so each chunk opens on its own keyframe (no black
        #     flash), audio to AAC (avoids A/V offset that caused the jump)
        # Native ffmpeg makes this fast; this is the whole reason we left wasm.
        subprocess.run(
            ["ffmpeg", "-y",
             "-ss", str(c.start), "-i", src_path,
             "-t", str(c.duration),
             "-vf", "setpts=PTS-STARTPTS",
             "-af", "aresample=async=1,asetpts=PTS-STARTPTS",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, check=True,
        )
        produced.append((out_name, out_path))
    return produced


# --- production-file dedupe -------------------------------------------------


def dedupe_guard(src_path: str, production: bool, force: bool):
    """Warn on a byte-for-byte repeat of a recent production video.

    Runs for both the GCS-object path and the multipart fallback — the caller
    passes the source once it is on local disk. Returns a Flask (body, status)
    tuple to BLOCK the split with a 409, or None to let it proceed.

    Fail-safe: any trouble fingerprinting or reaching the hash store just means
    "proceed". This must never block or fail a split.
    """
    if not production:
        return None
    try:
        digest = hashes.sha256_file(src_path)
    except Exception:
        return None  # can't fingerprint -> stay out of the way
    if not force and hashes.is_duplicate(digest):
        return jsonify(
            duplicate=True,
            message="This is a byte-for-byte match of a recently split "
                    "production video. Split anyway?",
        ), 409
    # Not a duplicate, or the user chose "Split anyway": record it and proceed.
    hashes.add_hash(digest)
    return None


# --- routes -----------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/config")
def config():
    return jsonify(gcs=bool(GCS_BUCKET), max_upload_bytes=app.config["MAX_CONTENT_LENGTH"])


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/signed-upload")
def signed_upload():
    if not GCS_BUCKET:
        return {"error": "Direct upload is not configured (no GCS_BUCKET)."}, 501

    data = request.get_json(silent=True) or {}
    ext = os.path.splitext(data.get("filename") or "")[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext or ""):
        ext = ".mp4"
    obj = f"uploads/{uuid.uuid4().hex}/input{ext}"

    try:
        url = signed_put_url(_bucket().blob(obj))
    except Exception as e:  # signing / permissions / bucket problems
        return {"error": "Could not create an upload URL.", "detail": str(e)[:300]}, 500

    return jsonify(
        url=url, object=obj, method="PUT",
        headers={"Content-Type": _UPLOAD_CT},
        expires_in=int(SIGN_TTL.total_seconds()),
    )


@app.post("/split")
def split():
    payload = request.get_json(silent=True) if request.is_json else None
    gcs_object = (payload or {}).get("object")

    src = payload if payload is not None else request.form
    prefix = safe_prefix(src.get("prefix", "clip"))
    chunk_len = parse_chunk_len(src.get("chunk_len"))
    production = parse_flag(src.get("production"), True)
    force = parse_flag(src.get("force"), False)

    work = tempfile.mkdtemp(prefix="chunk_")
    streaming = False
    try:
        cleanup_blob = None
        if gcs_object:
            if not GCS_BUCKET:
                return {"error": "Direct upload is not configured."}, 501
            src_blob = _bucket().blob(gcs_object)
            if not src_blob.exists():
                return {"error": "Upload not found — it may have expired. Try again."}, 404
            src_ext = os.path.splitext(gcs_object)[1].lower() or ".mp4"
            src_path = os.path.join(work, "input" + src_ext)
            src_blob.download_to_filename(src_path)
            cleanup_blob = src_blob
        else:
            f = request.files.get("file")
            if not f or not f.filename:
                return {"error": "No file uploaded."}, 400
            src_ext = os.path.splitext(f.filename)[1].lower() or ".mp4"
            src_path = os.path.join(work, "input" + src_ext)
            f.save(src_path)

        # Production-file duplicate warning. Shared for both source paths; a 409
        # here leaves the uploaded GCS object in place so "Split anyway" (force=1)
        # can resubmit it — the bucket lifecycle rule reaps it otherwise.
        blocked = dedupe_guard(src_path, production, force)
        if blocked is not None:
            return blocked

        out_dir = os.path.join(work, "out")
        os.makedirs(out_dir, exist_ok=True)

        try:
            produced = split_video(src_path, prefix, chunk_len, out_dir)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or b"").decode("utf-8", "replace")[-800:]
            return {"error": "ffmpeg failed", "detail": msg}, 500

        # Build the zip on disk (keeps instance memory flat for big outputs),
        # staggering modified-times by one minute per chunk so date-modified
        # order matches chunk order (e.g. iOS AirDrop).
        zip_path = os.path.join(work, f"{prefix}.zip")
        base_time = datetime.now() - timedelta(minutes=len(produced))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for i, (name, path) in enumerate(produced):
                ts = (base_time + timedelta(minutes=i)).timestamp()
                os.utime(path, (ts, ts))
                z.write(path, arcname=name)

        # Stream it back. Chunked transfer encoding is exempt from Cloud Run's
        # 32 MiB HTTP/1 response cap, so the zip can be any size. Cleanup rides
        # on the generator finishing (or the client hanging up).
        def _stream():
            try:
                with open(zip_path, "rb") as fh:
                    while True:
                        block = fh.read(1024 * 1024)
                        if not block:
                            break
                        yield block
            finally:
                shutil.rmtree(work, ignore_errors=True)
                if cleanup_blob is not None:
                    try:
                        cleanup_blob.delete()
                    except Exception:
                        pass

        resp = Response(_stream(), mimetype="application/zip")
        resp.headers["Content-Disposition"] = f'attachment; filename="{prefix}.zip"'
        streaming = True
        return resp
    finally:
        if not streaming:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    # Cloud Run provides PORT; default 8080 for local runs.
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
