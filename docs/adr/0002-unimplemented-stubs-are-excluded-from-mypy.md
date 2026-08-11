# ADR-0002: Unimplemented stub modules are excluded from mypy

- **Status:** accepted
- **Date:** 2026-08-11
- **Milestone:** M1

## Context

The architecture phase created ~150 named stub modules ahead of implementation.
Each stub imports the symbols it will eventually use — `AgentState`,
`get_current_user`, `ApprovalRead` — but those symbols do not exist yet,
because the modules that define them are themselves stubs.

Running the M1 gate exposed the consequence:

```
Found 134 errors in 74 files (checked 151 source files)
```

Every one came from a stub referring to a future symbol. None came from
implemented code. The stubs already carried `# ruff: noqa: F401` for the same
reason, so ruff was handled and mypy was not.

This mattered because `make check` is the definition of done for every
milestone and the exact gate CI runs. A gate that cannot pass until milestone
16 provides no signal at milestones 1 through 15 — its failure output looks
identical whether or not you just broke something.

## Decision

Each not-yet-implemented module carries a two-line header:

```python
# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
```

The pragma is removed as part of implementing the module. It is deliberately
per-file rather than a path list in `pyproject.toml`.

## Consequences

**Good.** `make check` is green today and stays meaningful: a failure now means
you broke something. Type-check coverage grows monotonically as milestones
land — 77 of 151 modules are checked at M1. The pragma sits in the file being
implemented, so it is impossible to forget in the way a central exclude list is
forgettable.

**Bad.** A pragma left behind silently disables type checking for a module that
*is* implemented. Mitigation: removing it is part of each milestone's
definition of done, and it appears in the diff of the file you are working on.

**Rejected: `exclude` in `pyproject.toml`.** One central list, edited far from
the code it describes, is the classic stale-config failure. It also makes every
milestone touch a shared file for no reason.

**Rejected: deleting the stub imports.** The imports are documentation — they
state what a module will depend on. Deleting them discards the architecture
work the stubs exist to capture.

**Rejected: dropping mypy strict.** Strict typing is a stated project decision.
Weakening the whole project to accommodate placeholder files inverts the
tradeoff.

## Verification

```
$ make check
ruff check .            All checks passed!
ruff format --check .   160 files already formatted
mypy app                Success: no issues found in 151 source files
pytest                  12 passed
```
