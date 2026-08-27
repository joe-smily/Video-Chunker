"""
Video Chunker — Cloud Run service.

Native ffmpeg does the splitting server-side, so it's fast (seconds, not the
minutes ffmpeg.wasm took in the browser) and the seams are frame-perfect. The
browser only uploads the file and downloads the resulting zip.

Endpoints:
  GET  /          -> the upload page (index.html)
  GET  /healthz   -> health check for Cloud Run
  POST /split     -> multipart upload (file, prefix, chunk_len); returns a zip

Access is gated by Cloud Run IAM (see deploy notes), so only signed-in members
of your Workspace domain can reach it. There is no app-level auth here on
purpose — IAM is the wall, the same way the Apps Script deploy setting was.
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta

from flask import Flask, request, send_file, Response, render_template

from chunking import compute_chunks

app = Flask(__name__)

# Cap upload size so a runaway file can't exhaust the instance. 2 GB default;
# raise if your source videos are larger (Cloud Run request bodies can be big,
# but keep an eye on instance memory/disk).
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024))

# A safe filename prefix: letters, numbers, dash, underscore only.
_SAFE_PREFIX = re.compile(r"[^A-Za-z0-9_-]")


def safe_prefix(raw: str) -> str:
    p = _SAFE_PREFIX.sub("", (raw or "").strip()) or "clip"
    return p[:60]


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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/split")
def split():
    f = request.files.get("file")
    if not f or not f.filename:
        return {"error": "No file uploaded."}, 400

    prefix = safe_prefix(request.form.get("prefix", "clip"))
    try:
        chunk_len = float(request.form.get("chunk_len", "10") or "10")
        if chunk_len <= 0:
            chunk_len = 10.0
    except ValueError:
        chunk_len = 10.0

    work = tempfile.mkdtemp(prefix="chunk_")
    try:
        # Keep the original extension so ffmpeg picks the right demuxer.
        src_ext = os.path.splitext(f.filename)[1].lower() or ".mp4"
        src_path = os.path.join(work, "input" + src_ext)
        f.save(src_path)

        out_dir = os.path.join(work, "out")
        os.makedirs(out_dir, exist_ok=True)

        try:
            produced = split_video(src_path, prefix, chunk_len, out_dir)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or b"").decode("utf-8", "replace")[-800:]
            return {"error": "ffmpeg failed", "detail": msg}, 500

        # Build the zip in memory, staggering modified-times by one minute per
        # chunk so date-modified order matches chunk order (e.g. iOS AirDrop).
        mem = io.BytesIO()
        base_time = datetime.now() - timedelta(minutes=len(produced))
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_STORED) as z:
            for i, (name, path) in enumerate(produced):
                dt = base_time + timedelta(minutes=i)
                zinfo = zipfile.ZipInfo(name, date_time=dt.timetuple()[:6])
                with open(path, "rb") as fh:
                    z.writestr(zinfo, fh.read())
        mem.seek(0)
        return send_file(
            mem, mimetype="application/zip",
            as_attachment=True, download_name=f"{prefix}.zip",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    # Cloud Run provides PORT; default 8080 for local runs.
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
