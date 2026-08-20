"""GitHub REST client — read-only at M14.

Layer: integrations. Returns our shapes, never GitHub's.

`X-GitHub-Api-Version` pins the response shape
----------------------------------------------
Unlike Notion's missing version header, omitting this one is **not an error**.
GitHub serves the current default version instead, and changes that default on a
schedule it publishes and nobody here watches. The failure mode is therefore not
a 400 on deploy day — it is a field quietly changing meaning months later, in
production, with a green test suite. Pinning is what turns a future surprise into
a future decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.integrations.base import BaseClient, OAuthError

REPOS_URL = "https://api.github.com/user/repos"

MAX_RESULTS = 30


@dataclass(frozen=True)
class GitHubRepository:
    """One repository, reduced to what this product uses."""

    full_name: str
    description: str | None
    private: bool
    url: str
    updated_at: datetime | None


class GitHubClient(BaseClient):
    """Lists repositories visible to the connected account."""

    extra_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    forbidden_message = (
        "This GitHub account did not grant access to that resource. "
        "Reconnect it, or check the repository's permissions."
    )

    async def list_repositories(
        self, access_token: str, *, limit: int = MAX_RESULTS
    ) -> list[GitHubRepository]:
        """Repositories for the connected account, most recently updated first.

        **Public repositories only**, and that is a consequence of the scope
        rather than of this method: M14 requests `read:user` and no repository
        scope, because GitHub's classic OAuth has no read-only grant for private
        code — see `oauth.py`. A user who expects their private repositories here
        is seeing the cost of not having asked for write access to all of them.
        """
        items = await self._get_list(
            REPOS_URL,
            access_token=access_token,
            params={"sort": "updated", "per_page": min(limit, MAX_RESULTS)},
        )
        return [_as_repository(item) for item in items]

    async def _get_list(
        self, url: str, *, access_token: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """GET an endpoint whose top level is a JSON *array*.

        `BaseClient.get_json` is typed and documented as returning an object,
        which is true of every Google, Notion, Slack and Stripe endpoint used
        here. GitHub's collection endpoints return a bare list, so parsing it
        through `get_json` would be a lie about the shape that mypy cannot catch
        at runtime — the annotation would say `dict` while a `list` flowed
        through, and the first `.get()` would raise `AttributeError`.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.get(url, params=params, headers=self._headers(access_token))
            except httpx.HTTPError as error:
                message = f"Request to {url} failed: {error}"
                raise OAuthError(message) from error

        self._raise_for_status(response, url)

        payload: Any = response.json()

        if not isinstance(payload, list):
            message = f"GitHub returned {type(payload).__name__} where a list was expected."
            raise OAuthError(message)

        return [item for item in payload if isinstance(item, dict)]


def _as_repository(item: dict[str, Any]) -> GitHubRepository:
    """Translate one GitHub repository resource into ours."""
    updated = item.get("updated_at")

    return GitHubRepository(
        full_name=str(item.get("full_name", "")),
        description=item.get("description"),
        private=bool(item.get("private", False)),
        url=str(item.get("html_url", "")),
        # GitHub sends `2026-08-20T09:00:00Z`. `fromisoformat` handles the 'Z'
        # suffix from Python 3.11 onward; this project is 3.13, so no substitution
        # is needed — noted because the `.replace("Z", "+00:00")` workaround is
        # still the reflex, and it is now dead code wherever it appears.
        updated_at=datetime.fromisoformat(str(updated)) if updated else None,
    )
