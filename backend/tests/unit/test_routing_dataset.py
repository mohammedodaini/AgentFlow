"""The routing dataset refuses to produce a meaningless score (M15).

Same argument as `datasets.py` makes for the RAG golden set, and worth repeating
because the failure mode is silent: a green eval run over nothing is
indistinguishable, in CI, from a green eval run over everything.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.evaluation.datasets import DatasetError
from app.evaluation.routing import load_routing_dataset


def write(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    (tmp_path / "broken.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("app.evaluation.routing.DATA_ROOT", tmp_path)
    load_routing_dataset.cache_clear()


def test_a_missing_dataset_is_an_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.evaluation.routing.DATA_ROOT", tmp_path)
    load_routing_dataset.cache_clear()

    with pytest.raises(DatasetError, match="No routing dataset"):
        load_routing_dataset("broken")


def test_malformed_json_is_an_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("app.evaluation.routing.DATA_ROOT", tmp_path)
    load_routing_dataset.cache_clear()

    with pytest.raises(DatasetError, match="not valid JSON"):
        load_routing_dataset("broken")


def test_an_empty_dataset_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run over nothing scores 1.000 — the most dangerous green build there is."""
    write(tmp_path, monkeypatch, {"name": "broken", "examples": []})

    with pytest.raises(DatasetError, match="no examples"):
        load_routing_dataset("broken")


def test_duplicate_ids_are_refused(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One would overwrite the other in a report, and the set would silently
    measure nineteen things while claiming twenty."""
    write(
        tmp_path,
        monkeypatch,
        {
            "name": "broken",
            "examples": [
                {"id": "a", "instruction": "hello", "expected_agent": "none"},
                {"id": "a", "instruction": "hi", "expected_agent": "none"},
            ],
        },
    )

    with pytest.raises(DatasetError, match="duplicate example id"):
        load_routing_dataset("broken")


def test_an_unknown_agent_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked against `AGENT_NAMES`, so renaming an agent breaks the dataset
    loudly rather than scoring every example against a label nothing produces."""
    write(
        tmp_path,
        monkeypatch,
        {
            "name": "broken",
            "examples": [
                {"id": "a", "instruction": "hello", "expected_agent": "none"},
                {"id": "b", "instruction": "x", "expected_agent": "telepathy"},
            ],
        },
    )

    with pytest.raises(DatasetError, match="not in AGENT_NAMES"):
        load_routing_dataset("broken")


def test_a_plan_step_naming_no_agent_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        monkeypatch,
        {
            "name": "broken",
            "examples": [
                {"id": "a", "instruction": "hello", "expected_agent": "none"},
                {
                    "id": "b",
                    "instruction": "x",
                    "expected_agent": "rag",
                    "expected_plan": ["rag", "telepathy"],
                },
            ],
        },
    )

    with pytest.raises(DatasetError, match="not an agent"):
        load_routing_dataset("broken")


def test_a_dataset_with_no_refusals_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The check most routing evaluations leave out.**

    A set made only of routable instructions cannot tell a good router from one
    that always picks something — and "always picks something" is the default
    behaviour of every classifier ever written.
    """
    write(
        tmp_path,
        monkeypatch,
        {
            "name": "broken",
            "examples": [
                {"id": "a", "instruction": "How are expenses paid?", "expected_agent": "rag"}
            ],
        },
    )

    with pytest.raises(DatasetError, match="cannot detect a router that always chooses"):
        load_routing_dataset("broken")


def test_the_shipped_dataset_loads() -> None:
    load_routing_dataset.cache_clear()
    dataset = load_routing_dataset()

    assert dataset.name == "routing"
    assert dataset.examples
