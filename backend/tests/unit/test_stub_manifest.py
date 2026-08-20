"""The coverage omit list must describe reality (M4).

A coverage gate is only worth having if the number it guards means something.
Roughly two thirds of `app/` is still scaffolding — modules with a docstring,
some imports and a milestone TODO — so measuring the whole package would report
how much of the roadmap exists rather than how well the built part is tested.
Those modules are therefore omitted in `pyproject.toml`.

An omit list is exactly the kind of central configuration that rots. Implement
a module, forget to delist it, and its coverage silently stops being measured —
the gate keeps passing while real code goes untested, which is worse than
having no gate at all.

So the list is not trusted; it is checked, in both directions, against the
source tree. The signal is deliberately crude and hard to fake: a module that
defines no class and no function is scaffolding. Implementing anything gives it
a `def` or a `class`, and these tests fail until the omit entry is removed.

Same idea as ADR-0002, which requires deleting a module's
`# mypy: ignore-errors` pragma as part of implementing it — with the difference
that this one cannot be forgotten, because a test enforces it.
"""

from __future__ import annotations

import ast
import fnmatch
import pathlib
import tomllib

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"

DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

MINIMUM_EXPECTED_IMPLEMENTED = 30

MINIMUM_EXPECTED_STUBS = 9
"""Lowered from 30 at M12 and from 20 at M14, and the reason matters more than the
number.

These bounds exist to catch a *broken classifier* — one that suddenly called
everything implemented would make the two real tests above pass while measuring
nothing. They were never meant to describe the roadmap, and the docstring below
says so.

They tracked it anyway, because when they were written most of the project was
scaffolding. Twelve of sixteen milestones later the tree is 104 implemented modules
to 28 stubs, and the old floor of 30 had started failing on *success*. Moving it is
the honest response; deleting the assertion would throw away the only thing guarding
the classifier.

M14 implemented twelve of them — five providers' OAuth and clients, the shared
Google flow, and `app/agents/email/` — taking the tree to 17 stubs, so the floor
moved again. It is now well clear of what remains rather than tracking it, which
is what it should have been all along.

M15 implemented the supervisor and the planner and left the other four alone,
each for a reason: `evaluation/` and `memory/` are graphs that were never needed
(M8 put evaluation in a runner, M10 put extraction in a worker task — neither has
a branch to be a graph about), `research/` needs a web search tool this
environment cannot have, and `proposal/` is a template renderer nobody has asked
for.

What remains is those four, M16 (rate limiting, metrics, the audit log), and
`app/integrations/google_drive/`, which M14 left alone because nothing in this
product reads a file from Drive.
"""

DEFINITION_FREE_BUT_IMPLEMENTED = frozenset(
    {
        # Pure wiring: builds the v1 router by including sub-routers. It has no
        # function to define, and it is genuinely finished. The only entry that
        # has ever needed to be here — keep it that way, and require a written
        # reason for any addition.
        "app/api/v1/router.py",
    }
)


def _omit_patterns() -> list[str]:
    with (BACKEND_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    patterns: list[str] = config["tool"]["coverage"]["run"]["omit"]
    return patterns


def _is_omitted(relative_path: str, patterns: list[str]) -> bool:
    """Match coverage's own glob semantics closely enough to be useful.

    coverage translates `**/` to "zero or more directories", which fnmatch does
    not do — so each pattern is also tried with that segment removed.
    """
    return any(
        fnmatch.fnmatch(relative_path, pattern)
        or fnmatch.fnmatch(relative_path, pattern.replace("**/", ""))
        for pattern in patterns
    )


def _module_paths() -> list[pathlib.Path]:
    """Every module in `app/` except package markers.

    `__init__.py` files are empty here, so they carry no statements and cannot
    move the coverage number in either direction.
    """
    return sorted(path for path in APP_ROOT.rglob("*.py") if path.name != "__init__.py")


def _is_stub(path: pathlib.Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return not any(isinstance(node, DEFINITIONS) for node in tree.body)


def _classify() -> tuple[set[str], set[str]]:
    """Split `app/` into (stubs, implemented), by repo-relative posix path."""
    stubs: set[str] = set()
    implemented: set[str] = set()

    for path in _module_paths():
        relative = path.relative_to(BACKEND_ROOT).as_posix()

        if relative in DEFINITION_FREE_BUT_IMPLEMENTED or not _is_stub(path):
            implemented.add(relative)
        else:
            stubs.add(relative)

    return stubs, implemented


def test_every_stub_module_is_omitted_from_coverage() -> None:
    """A new stub must be delisted, or it drags the measured number down.

    This is the direction that keeps the gate honest *upward*: without it,
    adding scaffolding for M9 would quietly lower coverage, and someone would
    respond by lowering `fail_under`.
    """
    patterns = _omit_patterns()
    stubs, _ = _classify()

    missing = sorted(path for path in stubs if not _is_omitted(path, patterns))

    assert not missing, (
        "These modules define no class or function, so they are scaffolding, "
        "but coverage still measures them. Add them to "
        "[tool.coverage.run] omit in pyproject.toml:\n  " + "\n  ".join(missing)
    )


def test_no_implemented_module_is_omitted_from_coverage() -> None:
    """The direction that actually matters.

    An implemented module left on the omit list is untested code reporting as
    nothing at all. This is the failure a coverage gate exists to prevent, and
    the one a hand-maintained list produces by default.
    """
    patterns = _omit_patterns()
    _, implemented = _classify()

    wrongly_omitted = sorted(path for path in implemented if _is_omitted(path, patterns))

    assert not wrongly_omitted, (
        "These modules are implemented but excluded from coverage, so nothing "
        "measures them. Remove them from [tool.coverage.run] omit in "
        "pyproject.toml:\n  " + "\n  ".join(wrongly_omitted)
    )


def test_the_manifest_still_describes_a_mostly_unbuilt_project() -> None:
    """A sanity check on the checker itself.

    If a refactor ever broke `_is_stub` so that everything looked implemented,
    both tests above would pass while measuring nothing useful. These bounds
    are loose on purpose — they catch a broken classifier, not a changed
    roadmap.
    """
    stubs, implemented = _classify()

    assert len(implemented) >= MINIMUM_EXPECTED_IMPLEMENTED
    assert len(stubs) >= MINIMUM_EXPECTED_STUBS
    assert "app/main.py" in implemented
    assert "app/services/organization_service.py" in implemented
    # A *still*-unbuilt module, re-chosen for the second time. M9 moved it off
    # `app/agents/rag/graph.py` and onto the supervisor, reasoning that M15 was
    # "the furthest away" — and M15 arrived, so this assertion fired again. That
    # is the canary working twice rather than a nuisance: it proves the classifier
    # still tells an implemented module from a stub.
    #
    # `proposal/` is the better choice precisely because no milestone claims it.
    # A canary pointing at scheduled work has an expiry date; this one only fires
    # if somebody builds a thing nobody planned.
    assert "app/agents/proposal/graph.py" in stubs
