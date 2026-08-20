"""Gmail API client — reads the mailbox, drafts, and sends an approved draft.

Layer: integrations. Returns our shapes, never Gmail's.

**base64url, not base64.** A message body is sent as `raw`, and Gmail requires
RFC 4648 §5 encoding — the alphabet using `-` and `_`. Standard base64 produces
`+` and `/`, which Gmail rejects with `400 Invalid value for ByteString`. The
catch is that most short messages contain neither character, so a draft built
with `b64encode` works in testing and fails on the first message whose bytes
happen to encode one. `urlsafe_b64encode` is the fix and the only correct choice.

Listing costs one request per message, and that is Gmail's design
-----------------------------------------------------------------
`users.messages.list` returns **ids only** — no subject, no sender, no date. Every
one of those requires a second call. There is no bulk variant outside the batch
endpoint, so a listing of ten messages is eleven requests, and this module says so
rather than hiding it behind a method that looks cheap. `MAX_RESULTS` is small for
exactly that reason.

`format=metadata` with an explicit header list is what keeps those calls small: it
returns the headers asked for and no body, so listing an inbox does not download
every attachment in it.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from app.integrations.base import BaseClient

API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGES_URL = f"{API_ROOT}/messages"
DRAFTS_URL = f"{API_ROOT}/drafts"
SEND_DRAFT_URL = f"{DRAFTS_URL}/send"

MAX_RESULTS = 10
"""Deliberately small. See the module docstring: this is N+1 by Gmail's design,
so the bound is a request budget rather than a page size."""


@dataclass(frozen=True)
class EmailMessageSummary:
    """One message, reduced to what this product uses."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime | None


@dataclass(frozen=True)
class EmailDraft:
    """A draft that exists in the user's Gmail account.

    `draft_id` and `message_id` are different strings for the same thing, and
    both are needed: sending takes the draft id, while a link to the drafted mail
    takes the message id. Storing only one means a second round trip later to
    recover the other.
    """

    draft_id: str
    message_id: str
    to: str
    subject: str
    body: str


class GmailClient(BaseClient):
    """Reads mail, creates drafts, and sends a draft that was approved."""

    forbidden_message = (
        "This Google account was connected without permission to manage mail. "
        "Reconnect it to grant access."
    )

    async def list_messages(
        self, access_token: str, *, query: str = "", limit: int = MAX_RESULTS
    ) -> list[EmailMessageSummary]:
        """Recent messages, newest first.

        `q` accepts Gmail's own search syntax (`is:unread`, `from:…`). Passed
        straight through rather than wrapped in a query builder: it is a
        well-documented language the user already knows, and a partial
        reimplementation of it would support less while looking like it supports
        more.
        """
        params: dict[str, Any] = {"maxResults": min(limit, MAX_RESULTS)}

        if query:
            params["q"] = query

        listing = await self.get_json(MESSAGES_URL, access_token=access_token, params=params)
        ids = [str(item["id"]) for item in listing.get("messages", []) if item.get("id")]

        # Concurrently, because these are independent GETs and doing them in
        # sequence makes a ten-message listing ten round trips deep. Bounded by
        # `MAX_RESULTS` above rather than by a semaphore — the bound has to exist
        # somewhere, and putting it on the request count is more honest than
        # letting the caller ask for 500 and throttling silently.
        details = await asyncio.gather(
            *(self._message(access_token, message_id) for message_id in ids)
        )
        return [_as_summary(detail) for detail in details]

    async def create_draft(
        self, access_token: str, *, to: str, subject: str, body: str
    ) -> EmailDraft:
        """Write a draft into the user's Gmail account.

        **Creating a draft is not sending one.** This is the half of the email
        agent that runs without approval, and it is safe precisely because a draft
        is invisible to the recipient — it sits in the user's own Drafts folder,
        where they can read, edit or delete it. The approval gates
        `send_draft`.
        """
        payload = await self.post_json(
            DRAFTS_URL,
            access_token=access_token,
            body={"message": {"raw": encode_message(to=to, subject=subject, body=body)}},
        )
        message = payload.get("message", {})

        return EmailDraft(
            draft_id=str(payload.get("id", "")),
            message_id=str(message.get("id", "")),
            to=to,
            subject=subject,
            body=body,
        )

    async def send_draft(self, access_token: str, *, draft_id: str) -> str:
        """Send an existing draft. **Only ever called on an approved action.**

        The single irreversible operation in this package. Sending takes the draft
        id and nothing else — the content that goes out is whatever is in the
        draft, which is the content the human read. Re-composing the message here
        from parameters would reintroduce exactly the gap ADR-0015 closed: what was
        approved and what was sent could differ.
        """
        payload = await self.post_json(
            SEND_DRAFT_URL, access_token=access_token, body={"id": draft_id}
        )
        return str(payload.get("id", ""))

    async def _message(self, access_token: str, message_id: str) -> dict[str, Any]:
        """One message's headers, without its body."""
        return await self.get_json(
            f"{MESSAGES_URL}/{message_id}",
            access_token=access_token,
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date"],
            },
        )


def encode_message(*, to: str, subject: str, body: str) -> str:
    """Build an RFC 2822 message and encode it the way Gmail requires.

    `EmailMessage` rather than an f-string, because a subject containing a
    non-ASCII character needs RFC 2047 encoding and a body needs a correct
    `Content-Type` — both of which the standard library gets right and hand-rolled
    header concatenation gets wrong in a way that reaches the recipient.

    The encoding is **base64url**. See the module docstring for why the ordinary
    one passes every casual test and then fails.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _as_summary(payload: dict[str, Any]) -> EmailMessageSummary:
    """Translate one Gmail message resource into ours."""
    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("payload", {}).get("headers", [])
        if isinstance(header, dict)
    }
    received = payload.get("internalDate")

    return EmailMessageSummary(
        message_id=str(payload.get("id", "")),
        thread_id=str(payload.get("threadId", "")),
        sender=headers.get("from", "(unknown sender)"),
        subject=headers.get("subject") or "(no subject)",
        snippet=str(payload.get("snippet", "")),
        # `internalDate` is a string of **milliseconds**, not seconds — Gmail is
        # the only provider here that does this. Dividing by 1000 in the wrong
        # place puts every message in the year 57000, which is at least obvious;
        # forgetting the `tz=UTC` is the quiet one, since it silently uses the
        # server's local zone.
        received_at=(datetime.fromtimestamp(int(received) / 1000, tz=UTC) if received else None),
    )
