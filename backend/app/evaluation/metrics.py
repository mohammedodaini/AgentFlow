"""Deterministic eval metrics — retrieval recall@k, MRR, citation accuracy.

Pure functions. Deterministic where possible; the LLM judge (judge.py) is
only for what code cannot measure.
"""

from __future__ import annotations

# TODO(M8): recall_at_k(retrieved_ids, relevant_ids, k), mrr(...)
