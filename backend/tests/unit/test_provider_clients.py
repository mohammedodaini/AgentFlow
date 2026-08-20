"""Translating five providers' payloads into ours (M14).

The boundary rule — provider types never leak upward — is only worth having if the
translation is correct, and each of these payloads has at least one trap:

- Slack reports `ok: false` inside an HTTP 200, so an empty channel list would
  otherwise stand in for an error.
- Notion has no `title` field; the title is inside a property whose *key* the user
  chose.
- GitHub's collection endpoints return a bare JSON array, not an object.
- Stripe sends integer minor units, and JPY has no minor unit.
- Gmail returns message ids only, and its timestamps are milliseconds.
"""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.integrations.base import OAuthError, OAuthRevokedError
from app.integrations.github.client import GitHubClient
from app.integrations.gmail.client import GmailClient, encode_message
from app.integrations.notion.client import NOTION_VERSION, NotionClient
from app.integrations.slack.client import SlackClient
from app.integrations.stripe.client import StripeClient

TOKEN = "test-access-token"  # noqa: S105 — synthetic


def transport_returning(payload: Any, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# -- Slack ----------------------------------------------------------------


async def test_slack_ok_false_is_an_error_not_an_empty_list() -> None:
    """**The trap.** `BaseClient` classifies by status code, and Slack's failure is
    a 200. Left alone this returns `[]`, which reads as "the workspace has no
    channels" — an integration that appears to work and reports nothing."""
    client = SlackClient(transport=transport_returning({"ok": False, "error": "invalid_auth"}))

    with pytest.raises(OAuthRevokedError):
        await client.list_channels(TOKEN)


async def test_slack_a_non_credential_error_stays_retryable() -> None:
    client = SlackClient(transport=transport_returning({"ok": False, "error": "ratelimited"}))

    with pytest.raises(OAuthError) as raised:
        await client.list_channels(TOKEN)

    assert not isinstance(raised.value, OAuthRevokedError)


async def test_slack_channels_are_translated() -> None:
    client = SlackClient(
        transport=transport_returning(
            {
                "ok": True,
                "channels": [
                    {
                        "id": "C1",
                        "name": "general",
                        "topic": {"value": "Anything"},
                        "num_members": 12,
                        "is_archived": False,
                    }
                ],
            }
        )
    )

    channels = await client.list_channels(TOKEN)

    assert channels[0].name == "#general"
    assert channels[0].topic == "Anything"
    assert channels[0].member_count == 12


async def test_slack_archived_channels_are_dropped() -> None:
    """An archived channel is exactly what an agent should never suggest posting
    to, and Slack returns them when asked despite `exclude_archived`."""
    client = SlackClient(
        transport=transport_returning(
            {
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "old", "is_archived": True},
                    {"id": "C2", "name": "new", "is_archived": False},
                ],
            }
        )
    )

    channels = await client.list_channels(TOKEN)

    assert [channel.name for channel in channels] == ["#new"]


async def test_slack_a_channel_with_no_topic_is_none_not_empty_string() -> None:
    client = SlackClient(
        transport=transport_returning({"ok": True, "channels": [{"id": "C1", "name": "x"}]})
    )

    assert (await client.list_channels(TOKEN))[0].topic is None


# -- Notion ---------------------------------------------------------------


def notion_page(title: str, *, key: str = "Name") -> dict[str, Any]:
    return {
        "object": "page",
        "id": "p-1",
        "url": "https://notion.test/p-1",
        "last_edited_time": "2026-08-20T09:00:00.000Z",
        "properties": {key: {"type": "title", "title": [{"plain_text": title}]}},
    }


async def test_notion_finds_the_title_by_type_not_by_key() -> None:
    """**The trap.** There is no `title` field: the title lives in a property whose
    key is the workspace's own column name. Looking up `properties["Name"]` works on
    a fresh workspace and fails on every real one."""
    client = NotionClient(
        transport=transport_returning({"results": [notion_page("Q3", key="Tâche")]})
    )

    pages = await client.search_pages(TOKEN)

    assert pages[0].title == "Q3"


async def test_notion_an_untitled_page_is_labelled() -> None:
    """An untitled page has a title property holding an empty list. "" renders as a
    blank row nobody can identify or click."""
    page = notion_page("")
    client = NotionClient(transport=transport_returning({"results": [page]}))

    assert (await client.search_pages(TOKEN))[0].title == "(untitled)"


async def test_notion_a_page_with_no_title_property_is_labelled() -> None:
    page = notion_page("x")
    page["properties"] = {"Status": {"type": "select"}}
    client = NotionClient(transport=transport_returning({"results": [page]}))

    assert (await client.search_pages(TOKEN))[0].title == "(untitled)"


async def test_notion_a_page_with_malformed_properties_is_labelled() -> None:
    page = notion_page("x")
    page["properties"] = []
    client = NotionClient(transport=transport_returning({"results": [page]}))

    assert (await client.search_pages(TOKEN))[0].title == "(untitled)"


async def test_notion_databases_are_filtered_out() -> None:
    """A search filtered to pages can still return a database whose parent matched.
    Rendering one as a page gives a row that opens onto something with no title."""
    client = NotionClient(
        transport=transport_returning(
            {"results": [{"object": "database", "id": "d-1"}, notion_page("Real")]}
        )
    )

    pages = await client.search_pages(TOKEN)

    assert [page.title for page in pages] == ["Real"]


async def test_notion_sends_the_version_header() -> None:
    """Without it Notion answers `400 validation_error`, which surfaces as a
    generic failed request — a 400 that looks like a bad query."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"results": []})

    await NotionClient(transport=httpx.MockTransport(handler)).search_pages(TOKEN)

    assert seen["notion-version"] == NOTION_VERSION


async def test_notion_a_query_is_forwarded() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"results": []})

    await NotionClient(transport=httpx.MockTransport(handler)).search_pages(TOKEN, query="budget")

    assert "budget" in seen["body"]


# -- GitHub ---------------------------------------------------------------


async def test_github_parses_a_bare_json_array() -> None:
    """**The trap.** GitHub's collection endpoints return a list at the top level,
    while `BaseClient.get_json` is typed and documented as returning an object. The
    annotation would say `dict` while a `list` flowed through it."""
    client = GitHubClient(
        transport=transport_returning(
            [
                {
                    "full_name": "ada/analytical-engine",
                    "description": "notes",
                    "private": False,
                    "html_url": "https://github.test/ada/analytical-engine",
                    "updated_at": "2026-08-20T09:00:00Z",
                }
            ]
        )
    )

    repositories = await client.list_repositories(TOKEN)

    assert repositories[0].full_name == "ada/analytical-engine"
    assert repositories[0].updated_at is not None


async def test_github_an_object_where_a_list_was_expected_is_an_error() -> None:
    client = GitHubClient(transport=transport_returning({"message": "Not Found"}))

    with pytest.raises(OAuthError):
        await client.list_repositories(TOKEN)


async def test_github_a_401_is_a_dead_credential() -> None:
    client = GitHubClient(transport=transport_returning([], status=401))

    with pytest.raises(OAuthRevokedError):
        await client.list_repositories(TOKEN)


async def test_github_a_403_says_reconnect_in_githubs_terms() -> None:
    """M11 hard-coded Google Calendar's 403 wording into the shared base class, so
    a 403 from any other provider would have told the user to reconnect a calendar
    they may never have connected."""
    client = GitHubClient(transport=transport_returning([], status=403))

    with pytest.raises(OAuthRevokedError) as raised:
        await client.list_repositories(TOKEN)

    assert "GitHub" in str(raised.value)
    assert "calendar" not in str(raised.value).lower()


async def test_github_a_transport_failure_is_transient() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = GitHubClient(transport=httpx.MockTransport(explode))

    with pytest.raises(OAuthError):
        await client.list_repositories(TOKEN)


async def test_github_sends_a_pinned_api_version() -> None:
    """Omitting it is *not* an error — GitHub serves whichever version it currently
    defaults to, and changes that on a date nobody here watches. The failure is a
    field quietly changing meaning in production with a green test suite."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[])

    await GitHubClient(transport=httpx.MockTransport(handler)).list_repositories(TOKEN)

    assert seen["x-github-api-version"] == "2022-11-28"


# -- Stripe ---------------------------------------------------------------


def charge(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ch_1",
        "amount": 2500,
        "currency": "gbp",
        "status": "succeeded",
        "description": "Subscription",
        "created": 1_755_680_400,
    }
    return base | overrides


async def test_stripe_minor_units_become_a_decimal() -> None:
    charges = await StripeClient(transport=transport_returning({"data": [charge()]})).list_charges(
        TOKEN
    )

    assert charges[0].amount == Decimal("25.00")
    assert isinstance(charges[0].amount, Decimal)


async def test_stripe_zero_decimal_currencies_are_not_divided() -> None:
    """**The trap worth the most money.** `amount: 2500` is ¥2,500 in JPY and
    £25.00 in GBP. A single `/ 100` reports the yen figure as ¥25 — a hundredfold
    error in a financial display, produced by code that looks obviously right."""
    charges = await StripeClient(
        transport=transport_returning({"data": [charge(currency="jpy")]})
    ).list_charges(TOKEN)

    assert charges[0].amount == Decimal(2500)
    assert charges[0].currency == "JPY"


async def test_stripe_timestamps_are_utc_aware() -> None:
    """`fromtimestamp` without a timezone returns a naive datetime in the *server's*
    local zone — UTC in a container and something else on a laptop, so the bug shows
    up in exactly one of the two environments."""
    charges = await StripeClient(transport=transport_returning({"data": [charge()]})).list_charges(
        TOKEN
    )

    assert charges[0].created_at.tzinfo is not None


async def test_stripe_has_no_write_method() -> None:
    """Read-only is enforced by the `read_only` scope on Stripe's side *and* by the
    absence of any method here that could do otherwise. Either alone would be a
    single point of failure for the worst outcome in this codebase."""
    methods = {name for name in dir(StripeClient) if not name.startswith("_")}

    assert methods & {"post_json"} == {"post_json"}  # inherited, and unused
    assert not {name for name in methods if name.startswith(("create", "update", "delete"))}


# -- Gmail ----------------------------------------------------------------


def gmail_message(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "m-1",
        "threadId": "t-1",
        "snippet": "Hello there",
        "internalDate": "1755680400000",
        "payload": {
            "headers": [
                {"name": "From", "value": "ada@example.test"},
                {"name": "Subject", "value": "Q3"},
            ]
        },
    }
    return base | overrides


async def test_gmail_listing_fetches_each_message() -> None:
    """Gmail's list endpoint returns ids only — no subject, no sender, no date.
    Eleven requests for ten messages, which is why `MAX_RESULTS` is small."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)

        if url.rstrip("?").endswith("/messages") or "maxResults" in url:
            return httpx.Response(200, json={"messages": [{"id": "m-1"}, {"id": "m-2"}]})

        return httpx.Response(200, json=gmail_message())

    messages = await GmailClient(transport=httpx.MockTransport(handler)).list_messages(TOKEN)

    assert len(messages) == 2
    assert len(calls) == 3
    assert messages[0].sender == "ada@example.test"
    assert messages[0].subject == "Q3"


async def test_gmail_timestamps_are_milliseconds() -> None:
    """Gmail is the only provider here that sends milliseconds. Treating them as
    seconds puts every message in the year 57000."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "maxResults" in str(request.url):
            return httpx.Response(200, json={"messages": [{"id": "m-1"}]})

        return httpx.Response(200, json=gmail_message())

    messages = await GmailClient(transport=httpx.MockTransport(handler)).list_messages(TOKEN)

    assert messages[0].received_at is not None
    assert messages[0].received_at.year == 2025


async def test_gmail_a_message_with_no_subject_is_labelled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "maxResults" in str(request.url):
            return httpx.Response(200, json={"messages": [{"id": "m-1"}]})

        return httpx.Response(200, json=gmail_message(payload={"headers": []}, internalDate=None))

    messages = await GmailClient(transport=httpx.MockTransport(handler)).list_messages(TOKEN)

    assert messages[0].subject == "(no subject)"
    assert messages[0].sender == "(unknown sender)"
    assert messages[0].received_at is None


async def test_gmail_a_query_is_forwarded() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"messages": []})

    await GmailClient(transport=httpx.MockTransport(handler)).list_messages(
        TOKEN, query="is:unread"
    )

    assert "is%3Aunread" in seen[0]


def test_a_message_is_encoded_base64url() -> None:
    """**The trap.** Gmail rejects standard base64 — the `+` and `/` characters —
    with `400 Invalid value for ByteString`. Most short messages contain neither, so
    a draft built with `b64encode` works in testing and fails on the first message
    whose bytes happen to encode one."""
    raw = encode_message(to="ada@example.test", subject="Q3", body="ÿÿÿ" * 40)

    assert "+" not in raw
    assert "/" not in raw
    assert b"ada@example.test" in base64.urlsafe_b64decode(raw)


def test_a_non_ascii_subject_survives_encoding() -> None:
    """RFC 2047 encoding, which `EmailMessage` gets right and header concatenation
    gets wrong in a way that reaches the recipient."""
    decoded = base64.urlsafe_b64decode(
        encode_message(to="ada@example.test", subject="Café résumé", body="hi")
    )

    assert b"Subject:" in decoded


async def test_gmail_creates_then_sends_a_draft() -> None:
    """The two-step flow, and the reason for it: a failed send leaves the message in
    the user's own Drafts folder rather than losing the text entirely."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)

        if url.endswith("/drafts/send"):
            return httpx.Response(200, json={"id": "sent-1"})

        return httpx.Response(200, json={"id": "draft-1", "message": {"id": "msg-1"}})

    client = GmailClient(transport=httpx.MockTransport(handler))
    draft = await client.create_draft(TOKEN, to="ada@example.test", subject="Q3", body="Ready.")
    sent = await client.send_draft(TOKEN, draft_id=draft.draft_id)

    assert draft.draft_id == "draft-1"
    assert draft.message_id == "msg-1"
    assert sent == "sent-1"
    assert seen[1].endswith("/drafts/send")


async def test_gmail_a_403_names_mail_not_the_calendar() -> None:
    client = GmailClient(transport=transport_returning({}, status=403))

    with pytest.raises(OAuthRevokedError) as raised:
        await client.list_messages(TOKEN)

    assert "mail" in str(raised.value).lower()
