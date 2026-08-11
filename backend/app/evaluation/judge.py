"""LLM-as-judge scoring: faithfulness, relevance, completeness (1-5 rubric).

Judge prompts live in app/prompts/evaluation/. Known biases (position,
verbosity) — mitigations documented where used.
"""

from __future__ import annotations

# TODO(M8): async judge_answer(question, answer, sources) -> JudgeScore
