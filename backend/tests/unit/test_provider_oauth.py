"""The four OAuth flows M14 added, and the trap in each one (M14).

Every provider here diverges from Google somewhere that a status-code-first
client gets wrong, and each divergence gets a test naming what would break:

- **Slack** reports failure with HTTP 200 and `{"ok": false}`.
- **GitHub** answers form-encoded unless asked for JSON, and also reports failure
  with HTTP 200.
- **Notion** wants the client credentials in an `Authorization: Basic` header, and
  issues no refresh token at all.
- **Stripe** hands back a live secret key, so the scope requested is the only
  thing bounding it.

`MockTransport` rather than a patched `httpx.AsyncClient`, for the reason
`test_calendar_client.py` gives: patching globally silences every HTTP call in the
process, including ones a test never meant to stub.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.integrations.base import (
    OAuthError,
    OAuthProvider,
    OAuthRevokedError,
    parse_scopes,
)
from app.integrations.github.oauth import GitHubOAuth
from app.integrations.notion.oauth import NotionOAuth
from app.integrations.slack.oauth import SlackOAuth
from app.integrations.stripe.oauth import StripeOAuth

REDIRECT = "https://app.example.test/api/v1/integrations/x/callback"


def settings() -> Settings:
    return Settings(
        slack_client_id="slack-id",
        slack_client_secret="slack-secret",
        notion_client_id="notion-id",
        notion_client_secret="notion-secret",
        github_client_id="gh-id",
        github_client_secret="gh-secret",
        stripe_client_id="ca_test",
        stripe_client_secret="sk_test_platform",
    )


def transport(
    handler: Any,
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def responding(payload: dict[str, Any], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# -- scope parsing --------------------------------------------------------


def test_comma_separated_scopes_are_split() -> None:
    """M11's bare `.split()` stored `chat:write,channels:read` as ONE scope.

    Nothing raises when that happens — `Integration.scopes` is display and audit
    data — so the damage is a permissions list that is wrong in the one place a
    human looks to see what they granted.
    """
    assert parse_scopes("chat:write,channels:read") == ["chat:write", "channels:read"]


def test_space_separated_scopes_still_work() -> None:
    """Google obeys RFC 6749 and must not regress while Slack is accommodated."""
    assert parse_scopes("a/calendar.events a/userinfo.email") == [
        "a/calendar.events",
        "a/userinfo.email",
    ]


def test_a_missing_scope_field_is_an_empty_list() -> None:
    """Notion sends no `scope` at all. `None` must not become `["None"]`."""
    assert parse_scopes(None) == []


# -- Slack ----------------------------------------------------------------


async def test_slack_reports_a_dead_code_with_http_200() -> None:
    """**The Slack trap.** A status-code-first classifier sees success here.

    Without the body check, this response reaches `TokenGrant.from_response` and
    raises `KeyError: 'access_token'` three frames from the provider that refused
    us — the exact failure `_token_request` exists to prevent.
    """
    oauth = SlackOAuth(settings(), transport=responding({"ok": False, "error": "invalid_code"}))

    with pytest.raises(OAuthRevokedError):
        await oauth.exchange_code(code="used-already", redirect_uri=REDIRECT)


async def test_slack_transient_errors_stay_retryable() -> None:
    """`ratelimited` is not in `REVOKED_ERRORS`, so it must not become permanent.

    Collapsing it would mark a working integration REVOKED because Slack was busy
    for a minute, and reconnecting is the only thing a user could then do.
    """
    oauth = SlackOAuth(settings(), transport=responding({"ok": False, "error": "ratelimited"}))

    with pytest.raises(OAuthError) as raised:
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert not isinstance(raised.value, OAuthRevokedError)


async def test_slack_grants_have_no_expiry_and_no_refresh_token() -> None:
    """The shape that broke M11's token store — see `test_perpetual_credentials`."""
    oauth = SlackOAuth(
        settings(),
        transport=responding(
            {
                "ok": True,
                "access_token": "xoxb-test",
                "scope": "channels:read,team:read",
                "team": {"id": "T1", "name": "Acme"},
            }
        ),
    )

    grant = await oauth.exchange_code(code="good", redirect_uri=REDIRECT)

    assert grant.refresh_token is None
    assert grant.expires_at is None
    assert grant.scopes == ["channels:read", "team:read"]
    assert grant.external_account_id == "Acme"


async def test_slack_falls_back_to_the_team_id_when_unnamed() -> None:
    """ "Connected to T024BE7LD" is unhelpful; None identifies nothing at all."""
    oauth = SlackOAuth(
        settings(),
        transport=responding({"ok": True, "access_token": "xoxb", "team": {"id": "T1"}}),
    )

    grant = await oauth.exchange_code(code="good", redirect_uri=REDIRECT)

    assert grant.external_account_id == "T1"


async def test_slack_authorize_url_uses_commas() -> None:
    """Slack's authorize endpoint wants a comma-delimited scope list, not spaces."""
    url = SlackOAuth(settings()).authorize_url(state="s", redirect_uri=REDIRECT)

    assert "scope=channels%3Aread%2Cteam%3Aread" in url


async def test_slack_unreachable_is_transient() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    oauth = SlackOAuth(settings(), transport=transport(explode))

    with pytest.raises(OAuthError):
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)


async def test_slack_a_200_with_no_token_is_an_error() -> None:
    oauth = SlackOAuth(settings(), transport=responding({"ok": True}))

    with pytest.raises(OAuthError):
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)


async def test_slack_an_http_error_that_is_not_json_is_reported() -> None:
    """A load balancer's HTML error page during an incident."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text="<html>upstream</html>")

    oauth = SlackOAuth(settings(), transport=transport(handler))

    with pytest.raises(OAuthError):
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)


async def test_slack_refresh_works_when_rotation_is_enabled() -> None:
    oauth = SlackOAuth(
        settings(),
        transport=responding(
            {"ok": True, "access_token": "xoxb-new", "expires_in": 43200, "team": {"name": "Acme"}}
        ),
    )

    grant = await oauth.refresh("xoxe-1-refresh")

    assert grant.access_token == "xoxb-new"
    assert grant.expires_at is not None


# -- GitHub ---------------------------------------------------------------


async def test_github_asks_for_json_explicitly() -> None:
    """**The GitHub trap.** Without `Accept: application/json` the token endpoint
    answers `access_token=gho_x&scope=…` as form data.

    Parsing that as JSON raises, `_json_or_empty` swallows it, and the error reads
    "GitHub's token response contained no access_token" — right provider, wrong
    cause, and no amount of checking the client secret fixes it.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Only the token exchange. The follow-up identity call to /user sends
        # GitHub's own media type, and capturing both would let the later request
        # overwrite the header this test is about.
        if "login/oauth" in str(request.url):
            seen.update(request.headers)
            return httpx.Response(200, json={"access_token": "gho_x", "scope": "read:user"})

        return httpx.Response(200, json={"login": "ada"})

    oauth = GitHubOAuth(settings(), transport=transport(handler))
    await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert seen["accept"] == "application/json"


async def test_github_reports_a_bad_code_with_http_200() -> None:
    oauth = GitHubOAuth(settings(), transport=responding({"error": "bad_verification_code"}))

    with pytest.raises(OAuthRevokedError):
        await oauth.exchange_code(code="stale", redirect_uri=REDIRECT)


async def test_github_an_unknown_error_code_stays_retryable() -> None:
    oauth = GitHubOAuth(settings(), transport=responding({"error": "server_error"}))

    with pytest.raises(OAuthError) as raised:
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert not isinstance(raised.value, OAuthRevokedError)


async def test_github_identifies_the_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.github.com/user" in str(request.url):
            return httpx.Response(200, json={"login": "ada"})

        return httpx.Response(200, json={"access_token": "gho_x", "scope": "read:user"})

    oauth = GitHubOAuth(settings(), transport=transport(handler))
    grant = await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.external_account_id == "ada"


async def test_github_a_failed_identity_lookup_does_not_lose_the_connection() -> None:
    """The address is for humans to read. Throwing away an authorization the user
    just completed because a secondary lookup timed out is much worse than a
    connection that cannot label itself."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.github.com/user" in str(request.url):
            raise httpx.ReadTimeout("slow", request=request)

        return httpx.Response(200, json={"access_token": "gho_x"})

    oauth = GitHubOAuth(settings(), transport=transport(handler))
    grant = await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.access_token == "gho_x"
    assert grant.external_account_id is None


async def test_github_a_500_from_the_identity_endpoint_is_also_survivable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "api.github.com/user" in str(request.url):
            return httpx.Response(500, json={})

        return httpx.Response(200, json={"access_token": "gho_x"})

    grant = await GitHubOAuth(settings(), transport=transport(handler)).exchange_code(
        code="c", redirect_uri=REDIRECT
    )

    assert grant.external_account_id is None


async def test_github_refresh_returns_the_new_refresh_token() -> None:
    """Unlike Google, GitHub *does* reissue one — and `_store_grant` writes what it
    is given rather than assuming Google's rule."""
    oauth = GitHubOAuth(
        settings(),
        transport=responding(
            {"access_token": "gho_new", "refresh_token": "ghr_new", "expires_in": 28800}
        ),
    )

    grant = await oauth.refresh("ghr_old")

    assert grant.refresh_token == "ghr_new"


async def test_github_unreachable_is_transient() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(OAuthError):
        await GitHubOAuth(settings(), transport=transport(explode)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_github_a_500_on_the_token_endpoint_is_transient() -> None:
    with pytest.raises(OAuthError):
        await GitHubOAuth(settings(), transport=responding({}, status=500)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_github_a_200_with_no_token_is_an_error() -> None:
    with pytest.raises(OAuthError):
        await GitHubOAuth(settings(), transport=responding({"token_type": "bearer"})).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


def test_github_requests_no_repository_scope() -> None:
    """**The scope decision, asserted rather than left in a docstring.**

    GitHub's classic OAuth has no read-only grant for private repositories: `repo`
    is read *and write* to code, issues and settings across everything the user can
    reach. M14 is a read-only milestone, so it asks for `read:user` and accepts
    seeing public repositories only. A future change that quietly adds `repo` to
    make the listing richer fails here.
    """
    assert GitHubOAuth(settings()).scopes == ["read:user"]


# -- Notion ---------------------------------------------------------------


async def test_notion_sends_credentials_as_basic_auth() -> None:
    """**The Notion trap.** It answers 401 for credentials in the body — where
    Google, Slack and GitHub all accept them — so getting the *location* of the
    secret wrong looks exactly like getting the secret itself wrong."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"access_token": "secret_x", "workspace_name": "Acme"})

    oauth = NotionOAuth(settings(), transport=transport(handler))
    await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert seen["authorization"].startswith("Basic ")


async def test_notion_grants_have_no_scopes() -> None:
    """Notion grants access per *page*, chosen in a picker. Inventing a scope label
    would put a string in `Integration.scopes` that Notion never issued and that no
    check could be made against."""
    oauth = NotionOAuth(
        settings(),
        transport=responding({"access_token": "secret_x", "workspace_name": "Acme"}),
    )

    grant = await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.scopes == []
    assert grant.refresh_token is None
    assert grant.expires_at is None
    assert grant.external_account_id == "Acme"


async def test_notion_refresh_refuses_without_making_a_request() -> None:
    """Notion has no refresh grant. Asking it would return `400 invalid_request`,
    which classifies as *transient* — inviting a retry of something that can never
    succeed."""

    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        message = "refresh must not reach the network"
        raise AssertionError(message)

    oauth = NotionOAuth(settings(), transport=transport(explode))

    with pytest.raises(OAuthRevokedError):
        await oauth.refresh("anything")


async def test_notion_reports_a_dead_credential() -> None:
    oauth = NotionOAuth(settings(), transport=responding({"error": "unauthorized"}, status=401))

    with pytest.raises(OAuthRevokedError):
        await oauth.exchange_code(code="c", redirect_uri=REDIRECT)


async def test_notion_falls_back_to_the_workspace_id() -> None:
    oauth = NotionOAuth(
        settings(), transport=responding({"access_token": "secret_x", "workspace_id": "w-1"})
    )

    grant = await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.external_account_id == "w-1"


async def test_notion_unreachable_is_transient() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(OAuthError):
        await NotionOAuth(settings(), transport=transport(explode)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_notion_a_500_is_transient() -> None:
    with pytest.raises(OAuthError):
        await NotionOAuth(settings(), transport=responding({}, status=500)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_notion_a_200_with_no_token_is_an_error() -> None:
    with pytest.raises(OAuthError):
        await NotionOAuth(settings(), transport=responding({"bot_id": "b"})).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


def test_notion_authorize_url_sets_owner_user() -> None:
    """Required, with exactly one legal value. Omitting it is a 400 the *user*
    sees, on Notion's page, before anything reaches us."""
    url = NotionOAuth(settings()).authorize_url(state="s", redirect_uri=REDIRECT)

    assert "owner=user" in url


# -- Stripe ---------------------------------------------------------------


def test_stripe_requests_read_only() -> None:
    """**The most consequential scope in the codebase.**

    A Stripe Connect access token is an ordinary `sk_live_…` secret key for the
    connected account. `read_write` would let this application create charges and
    issue refunds against somebody else's business; `read_only` is the entire
    boundary, and `StripeClient` having no non-GET method is the other half.
    """
    assert StripeOAuth(settings()).scopes == ["read_only"]


async def test_stripe_returns_the_connected_account_id() -> None:
    oauth = StripeOAuth(
        settings(),
        transport=responding(
            {
                "access_token": "sk_test_connected",
                "refresh_token": "rt_test",
                "stripe_user_id": "acct_123",
                "scope": "read_only",
            }
        ),
    )

    grant = await oauth.exchange_code(code="c", redirect_uri=REDIRECT)

    assert grant.external_account_id == "acct_123"
    assert grant.refresh_token == "rt_test"


async def test_stripe_authenticates_with_the_platform_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"access_token": "sk_x", "stripe_user_id": "acct_1"})

    await StripeOAuth(settings(), transport=transport(handler)).exchange_code(
        code="c", redirect_uri=REDIRECT
    )

    assert seen["authorization"] == "Bearer sk_test_platform"


async def test_stripe_reports_a_dead_code() -> None:
    oauth = StripeOAuth(settings(), transport=responding({"error": "invalid_grant"}, status=400))

    with pytest.raises(OAuthRevokedError):
        await oauth.exchange_code(code="used", redirect_uri=REDIRECT)


async def test_stripe_refresh_mints_a_new_key() -> None:
    oauth = StripeOAuth(
        settings(),
        transport=responding({"access_token": "sk_new", "stripe_user_id": "acct_1"}),
    )

    grant = await oauth.refresh("rt_old")

    assert grant.access_token == "sk_new"


async def test_stripe_unreachable_is_transient() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(OAuthError):
        await StripeOAuth(settings(), transport=transport(explode)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_stripe_a_500_is_transient() -> None:
    with pytest.raises(OAuthError):
        await StripeOAuth(settings(), transport=responding({}, status=500)).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


async def test_stripe_a_200_with_no_token_is_an_error() -> None:
    with pytest.raises(OAuthError):
        await StripeOAuth(settings(), transport=responding({"livemode": True})).exchange_code(
            code="c", redirect_uri=REDIRECT
        )


def test_stripe_authorize_url_carries_the_connect_application_id() -> None:
    url = StripeOAuth(settings()).authorize_url(state="s", redirect_uri=REDIRECT)

    assert "client_id=ca_test" in url
    assert "scope=read_only" in url


def test_every_provider_conforms_to_the_protocol() -> None:
    """`runtime_checkable`, so a missing method is a test failure rather than an
    `AttributeError` during a callback the user is watching."""
    for oauth in (
        SlackOAuth(settings()),
        NotionOAuth(settings()),
        GitHubOAuth(settings()),
        StripeOAuth(settings()),
    ):
        assert isinstance(oauth, OAuthProvider)
