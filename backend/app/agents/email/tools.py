"""Email tools — the second side effect, and the first irreversible one.

Layer: agents. M12 built the approval machinery around the calendar and left this
package a stub, saying why: there was no Gmail integration to draft into. M14
builds one, so this is the milestone that finishes M12's stated scope.

**Sending email is worse than writing a calendar event, and the design says so.**
A wrong calendar event can be deleted; the other attendees see it disappear. A
sent email is gone — it is in somebody else's inbox, forwarded, quoted, and
indexed. There is no unsend. So this is the one action in the codebase where the
*content* a human approves matters as much as the fact that they approved
something, which is why the whole message — recipient, subject and body — lives in
`approvals.requested_action` rather than a reference to it.

The same two-function shape as `calendar/tools.py`
--------------------------------------------------
`parse_draft_request` turns an instruction into a description of an email. It
touches nothing. `build_send_draft` returns the thing that actually reaches Gmail,
and the graph can only get to it on the resume path — after a row exists and a
human has decided on it.

Why it drafts and then sends, rather than sending directly
-----------------------------------------------------------
Gmail offers both. `drafts.create` followed by `drafts.send` costs one extra
request and buys a better failure mode: if the send fails — a network drop, a
rate limit — the message is sitting in the user's own Drafts folder, visible and
finishable by hand. `messages.send` failing leaves nothing at all, and the user is
told an email they approved did not go, with no way to recover the text.

Deterministic parsing, and the honest reason
--------------------------------------------
Composing prose from an instruction is exactly what a model is for, and there is
no key in this environment (ADR-0010). So the parser reads a strict form and
refuses everything looser, rather than inventing an email nobody wrote. A
half-understood instruction here does not produce a wrong meeting; it produces a
wrong message with the user's name on it, sent to a real person.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations import OAuthRegistry
from app.integrations.gmail.client import GmailClient
from app.models.integration import Provider
from app.services.integration_service import IntegrationService

logger = structlog.get_logger(__name__)

SEND_DRAFT = "send_email_draft"

PROPOSED_ACTION_KIND = "email.send_draft"
"""The `kind` discriminator, and the second value it has ever had.

M12 introduced the field with one value and said why: "so that the day a second
kind exists (an email draft, M14) nothing has to guess what an untagged blob
was." This is that day, and nothing had to guess.
"""

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 5000
"""Bounded because this text is stored in JSONB, rendered into an approval
summary, and eventually put in front of a model. An unbounded body is an
unbounded prompt."""

_RECIPIENT = re.compile(r"[\w.+-]+@[\w-]+\.[\w][\w.-]*[\w]")
"""One address, and no attempt at RFC 5322.

A full address parser accepts things nobody sends and this would still have to
reject — and the cost of being slightly too strict is a refusal the user can act
on, while the cost of being too loose is mail to the wrong place.
"""

_BODY = re.compile(r"\bsaying\b[\s:,-]*(?P<body>.+)", re.IGNORECASE | re.DOTALL)
_SUBJECT = re.compile(r"\babout\b[\s:,-]*(?P<subject>.+?)(?=\s+\bsaying\b|$)", re.IGNORECASE)


def parse_draft_request(instruction: str) -> dict[str, Any] | None:
    """Turn an instruction into a proposed email, or None if it cannot.

    The accepted form is `… <address> about <subject> saying <body>`. Both an
    address and a body are required; the subject falls back to a default, because
    an email with no subject is unhelpful but an email to nobody is impossible and
    an email with no body is not worth sending.

    Returns a plain dict: it goes into JSONB, in front of a human, and into the
    executor — the same object all three times (ADR-0015).
    """
    recipient = _RECIPIENT.search(instruction)

    if recipient is None:
        return None

    body_match = _BODY.search(instruction)

    if body_match is None:
        return None

    body = " ".join(body_match.group("body").split())[:MAX_BODY_LENGTH]

    if not body:
        return None

    # The subject is searched for only in the text *before* the body, so that an
    # address or the word "about" appearing inside the message cannot rewrite the
    # header. Reading the whole instruction would let the body decide what the
    # email claims to be about.
    subject_match = _SUBJECT.search(instruction[: body_match.start()])
    subject = (
        " ".join(subject_match.group("subject").split())[:MAX_SUBJECT_LENGTH]
        if subject_match
        else ""
    )

    return {
        "kind": PROPOSED_ACTION_KIND,
        "to": recipient.group(0),
        "subject": subject or "(no subject)",
        "body": body,
    }


def describe(action: dict[str, Any]) -> str:
    """The one line a human reads before deciding.

    Rendered from the action by code, never by a model — the ADR-0015 rule. It
    names the recipient and the subject, and deliberately does **not** try to
    summarise the body: a summary of an email is a second account of it that might
    not match, and the person approving needs the words that will actually be
    sent. The full body is in `requested_action`, which the API returns whole.
    """
    return f"Send an email to {action['to']} with the subject '{action['subject']}'"


def build_send_draft(
    session: AsyncSession,
    registry: OAuthRegistry,
    settings: Settings,
    organization_id: uuid.UUID,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """The executor. Reachable only on the resume path, after an approval.

    The tenant is closed over and never appears in the action — ADR-0012's rule,
    and the stakes are higher here than anywhere it has been applied before. A
    prompt-injected `organization_id` on a *read* tool leaks data; on this one it
    would send mail from another customer's account, over their name, to a
    recipient of the attacker's choosing.

    Deliberately not a `BaseTool`, for the reason `build_create_event` is not: a
    tool is something the graph may call, and this is not.
    """

    async def execute(action: dict[str, Any]) -> dict[str, Any]:
        integrations = IntegrationService(session, _no_redis(), registry, settings)
        client = GmailClient()

        async with integrations.using(organization_id, Provider.GMAIL) as access_token:
            draft = await client.create_draft(
                access_token,
                to=action["to"],
                subject=action["subject"],
                body=action["body"],
            )
            sent_id = await client.send_draft(access_token, draft_id=draft.draft_id)

        logger.info(
            "email.draft_sent",
            organization_id=str(organization_id),
            message_id=sent_id,
            # The recipient is logged; the body is not. Logs are read by people who
            # were never authorised to read this customer's mail, and a body in a
            # log line outlives every retention policy that applies to the mailbox.
            to=action["to"],
        )
        return {"message_id": sent_id, "draft_id": draft.draft_id, "to": action["to"]}

    return execute


def _no_redis() -> Any:
    """`IntegrationService` needs a Redis handle only for the OAuth *connect* flow,
    which nothing here performs — these calls reach `get_fresh_token` and nothing
    else.

    Passing None rather than threading a real client through every caller keeps the
    dependency honest about where it is actually used: a future path that reaches
    the state store from here fails loudly on None rather than quietly sharing a
    connection nobody meant to give it.
    """
    return None
