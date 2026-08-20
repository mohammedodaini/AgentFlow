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

AGENT_NAMES = frozenset({RAG_AGENT, CALENDAR_AGENT, EMAIL_AGENT})
"""Every agent this deployment can run. Validated at the API boundary, so an
unknown name is a 422 rather than a run row that exists and never executes."""
