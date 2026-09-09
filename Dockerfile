# Native ffmpeg is the entire reason for this service — it splits in seconds with
# frame-perfect seams, where ffmpeg.wasm in the browser took minutes.
FROM python:3.12-slim

# ffmpeg + ffprobe from Debian repos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py chunking.py hashes.py ./
COPY templates ./templates

# Cloud Run sets PORT; gunicorn serves the Flask app. One worker with a few
# threads is plenty at team scale; the long pole is ffmpeg, which uses the CPU
# directly. Generous timeout so large videos finish.
ENV PORT=8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 600 app:app
