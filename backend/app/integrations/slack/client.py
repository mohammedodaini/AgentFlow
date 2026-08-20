"""Slack Web API client — read-only at M14.

Layer: integrations. Returns our shapes, never Slack's.

`ok: false` is a 200 here too
-----------------------------
`BaseClient.get_json` classifies by status code, which is right for Google,
GitHub, Notion and Stripe and wrong for Slack: a request with a dead token comes
back `200 OK` with `{"ok": false, "error": "invalid_auth"}`. Left alone, that
would produce an empty channel list rather than an error — an integration that
appears to work and reports that the workspace has no channels.

So `_call` re-checks the parsed body. It cannot live in `BaseClient` without
imposing Slack's convention on four providers that do not share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.base import BaseClient, OAuthError, OAuthRevokedError

CONVERSATIONS_URL = "https://slack.com/api/conversations.list"

MAX_RESULTS = 100
"""One page. Slack paginates with a cursor; nothing here follows it, because an
agent prompt cannot afford an unbounded channel list and a workspace with 4,000
channels would produce one."""

REVOKED_ERRORS = {"invalid_auth", "token_revoked", "token_expired", "account_inactive"}
"""Errors meaning the credential is dead rather than the request malformed."""


@dataclass(frozen=True)
class SlackChannel:
    """One public channel, reduced to what this product uses."""

    channel_id: str
    name: str
    topic: str | None
    member_count: int | None
    is_archived: bool


class SlackClient(BaseClient):
    """Lists channels. Holds no credential of its own."""

    forbidden_message = (
        "This Slack workspace did not grant the permission this action needs. "
        "Reconnect it to grant the missing scope."
    )

    async def list_channels(
        self, access_token: str, *, limit: int = MAX_RESULTS
    ) -> list[SlackChannel]:
        """Public channels in the connected workspace.

        `exclude_archived` is sent as `"true"` rather than dropped, and archived
        channels are filtered again below. Slack honours the parameter, but an
        archived channel is precisely the kind of thing an agent should never
        suggest posting to, and one belt-and-braces line is cheaper than the
        conversation about why it did.
        """
        payload = await self._call(
            CONVERSATIONS_URL,
            access_token=access_token,
            params={
                "limit": min(limit, MAX_RESULTS),
                "exclude_archived": "true",
                # Public channels only. `private_channel` would require
                # `groups:read`, which is not requested — see SCOPES.
                "types": "public_channel",
            },
        )

        return [
            _as_channel(item) for item in payload.get("channels", []) if not item.get("is_archived")
        ]

    async def _call(self, url: str, *, access_token: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET, then check `ok` — because Slack's 200 means nothing on its own."""
        payload = await self.get_json(url, access_token=access_token, params=params)

        if payload.get("ok") is False:
            error_code = str(payload.get("error", "unknown"))

            if error_code in REVOKED_ERRORS:
                message = "Slack rejected the workspace credential. Reconnect the workspace."
                raise OAuthRevokedError(message)

            message = f"Slack refused the request ({error_code})."
            raise OAuthError(message)

        return payload


def _as_channel(item: dict[str, Any]) -> SlackChannel:
    """Translate one Slack conversation object into ours."""
    topic = item.get("topic")
    topic_value = topic.get("value") if isinstance(topic, dict) else None

    return SlackChannel(
        channel_id=str(item.get("id", "")),
        # Slack sends the name without the leading '#', and every human writes it
        # with one. Adding it here keeps that formatting decision out of the four
        # places that render a channel.
        name=f"#{item.get('name', 'unknown')}",
        topic=str(topic_value) if topic_value else None,
        member_count=item.get("num_members"),
        is_archived=bool(item.get("is_archived", False)),
    )
