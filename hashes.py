"""
Recent-upload fingerprint store for the "production video" duplicate warning.

A single JSON object (``hashes.json``) in the existing ``GCS_BUCKET`` holds an
ordered list of up to 30 SHA-256 hex digests, newest first. No dates, no user
info. When a new digest is prepended and the list passes 30, the oldest entry
(at the end) drops off. Duplicate digests are allowed in the list — proceeding
past a warning just prepends the same digest again.

Fail-safe is the whole point: every read and write swallows errors. If the
bucket or object is unreachable the dedupe check is silently skipped and the
split proceeds normally. Nothing in here may ever block or fail a video split.

Concurrency: this is a read-modify-write of one object with no locking, so two
uploads landing at the same instant can rarely drop a hash. Accepted.
"""

import hashlib
import json

_OBJECT = "hashes.json"
_MAX = 30
_CHUNK = 1024 * 1024


def sha256_file(path: str) -> str:
    """SHA-256 hex digest of a file, read a MiB at a time (never fully in RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _blob():
    # Reuse the app's storage client / bucket helper. Imported lazily to avoid a
    # circular import at module load (app imports this module).
    from app import _bucket
    return _bucket().blob(_OBJECT)


def _read() -> list:
    """The stored list, newest first. Returns [] on any failure."""
    try:
        raw = _blob().download_as_bytes()
        data = json.loads(raw)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, str)]
    except Exception:
        pass
    return []


def _write(hashes: list) -> None:
    """Replace the stored list. Best-effort; failures are swallowed."""
    try:
        _blob().upload_from_string(json.dumps(hashes), content_type="application/json")
    except Exception:
        pass


def is_duplicate(digest: str) -> bool:
    """True if `digest` is already in the recent list. False on any failure."""
    try:
        return digest in _read()
    except Exception:
        return False


# --- operator / admin helpers --------------------------------------------------
#
# Unlike the functions above, these do NOT swallow store errors: the break-glass
# admin panel needs to tell "the list is empty" apart from "the bucket is
# unreachable", and a failed removal must surface rather than pass silently.


def snapshot() -> list:
    """The stored list, newest first. Returns [] if the object does not exist;
    raises if the bucket is unreachable or GCS_BUCKET is unset."""
    blob = _blob()
    if not blob.exists():
        return []
    data = json.loads(blob.download_as_bytes())
    if isinstance(data, list):
        return [x for x in data if isinstance(x, str)]
    return []


def remove_at(index: int, expected_digest: str) -> list:
    """Delete one entry by position, but only if it still holds `expected_digest`
    (guards against a concurrent add_hash having shifted the list). Raises
    ValueError('stale') on a mismatch, or the underlying error on store failure.
    Returns the new list."""
    current = snapshot()
    if not (0 <= index < len(current)) or current[index] != expected_digest:
        raise ValueError("stale")
    del current[index]
    _blob().upload_from_string(json.dumps(current), content_type="application/json")
    return current


def add_hash(digest: str) -> None:
    """Prepend `digest`, trim to the newest 30, write back. Best-effort."""
    try:
        hashes = _read()
        hashes.insert(0, digest)
        del hashes[_MAX:]
        _write(hashes)
    except Exception:
        pass
