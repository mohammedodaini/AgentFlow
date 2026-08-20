"""The agent registry.

Layer: agents. `agent_runs.agent_name` is a string rather than a database enum,
so adding an agent is a deploy rather than a migration. The trade is that a typo
becomes a run nobody can find — this is the list that closes it.

One entry today. `docs/agents.md` describes a supervisor and seven specialists;
M15 builds them, and only where the single agent measurably falls short. A
registry with one row looks like over-engineering and is not: it is the seam
that makes the second agent an addition rather than a refactor.
"""

from __future__ import annotations

RAG_AGENT = "rag"
"""Answers questions over the organization's own documents, with citations."""

CALENDAR_AGENT = "calendar"
"""M12: proposes calendar changes, and executes them only after a human approves.

The second agent, and the first with a side effect anyone outside this system can
see. It arrived in the milestone that built `approvals` rather than in M11, which
connected the calendar — because a write with nothing to authorise it is precisely
what the approval machinery exists to prevent.
"""

EMAIL_AGENT = "email"
"""M14: drafts an email and sends it only after a human approves the exact text.

The third agent, and the one M12 deferred — it said so plainly: "there is no Gmail
integration to draft into, and building one is M14's OAuth work". The approval
machinery needed no changes to accept it, which is the claim ADR-0015 made and
this is the test of it.

It is also the first *irreversible* side effect in the system. A calendar event can
be deleted; a sent email cannot be recalled from somebody else's inbox.
"""

SUPERVISOR_AGENT = "supervisor"
"""M15: the single entry point that decides which specialist takes the work.

The fourth agent, and the first that does not do any work itself. Until M15 a
user had to know which endpoint to call — `/ask` could not schedule and
`/agent-runs/calendar` could not answer a question — which made the human the
router.

`docs/agents.md` allows this package only when "a single agent measurably fails
at the breadth of tasks". It does, and the measurement is committed:
`app/evaluation/data/routing.json` scores the single-agent world at 0.300 and the
supervisor at 1.000 over twenty hand-written instructions, re-checked by every
`make eval`.
"""

AGENT_NAMES = frozenset({RAG_AGENT, CALENDAR_AGENT, EMAIL_AGENT, SUPERVISOR_AGENT})
"""Every agent this deployment can run. Validated at the API boundary, so an
unknown name is a 422 rather than a run row that exists and never executes."""
