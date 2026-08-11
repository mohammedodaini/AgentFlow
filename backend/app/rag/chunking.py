"""Text → chunks. Pure function — trivially unit-testable, no I/O.

Chunking quality caps retrieval quality; we will tune size/overlap at M8
using eval metrics, not vibes.
"""

from __future__ import annotations

# TODO(M6): chunk_text(text, *, chunk_size, overlap) -> list[Chunk]
#           (token-based sizing, paragraph-boundary preference)
