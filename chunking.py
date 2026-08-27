"""Chunk-boundary logic for splitting a video into ~10s segments.

Rule:
  - Chunks are 10s each.
  - The final remainder is absorbed into the last chunk when it is <= 5s
    (a 53s video -> last chunk is 13s: 40..53).
  - When the remainder is >= 6s it becomes its own chunk
    (a 57s video -> last chunk is 7s: 50..57).
"""

from dataclasses import dataclass
from typing import List


@dataclass
class Chunk:
    index: int      # 1-based
    start: float     # seconds
    duration: float  # seconds

    @property
    def end(self) -> float:
        return self.start + self.duration


def compute_chunks(
    duration: float,
    chunk_length: float = 10.0,
    absorb_threshold: float = 5.0,
) -> List[Chunk]:
    if not isinstance(duration, (int, float)) or duration != duration or duration <= 0:
        raise ValueError(f"Invalid duration: {duration!r}")

    # Whole video fits in a single chunk (one full chunk plus an absorbable
    # remainder, or shorter). Emit one chunk covering everything.
    if duration <= chunk_length + absorb_threshold:
        return [Chunk(index=1, start=0.0, duration=float(duration))]

    full_chunks = int(duration // chunk_length)
    remainder = round(duration - full_chunks * chunk_length, 6)

    base_chunks = full_chunks
    tail_start = None
    tail_duration = None

    if remainder == 0:
        tail_start = None  # clean multiple; all chunks exactly chunk_length
    elif remainder <= absorb_threshold:
        # Absorb remainder into the last full chunk.
        base_chunks = full_chunks - 1
        tail_start = base_chunks * chunk_length
        tail_duration = chunk_length + remainder
    else:
        # Remainder is its own chunk.
        tail_start = full_chunks * chunk_length
        tail_duration = remainder

    chunks: List[Chunk] = []
    idx = 1
    for i in range(base_chunks):
        chunks.append(Chunk(index=idx, start=i * chunk_length, duration=chunk_length))
        idx += 1
    if tail_start is not None:
        chunks.append(Chunk(index=idx, start=tail_start, duration=tail_duration))

    return chunks
