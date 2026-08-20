"""Notion API client — read-only at M14.

Layer: integrations. Returns our shapes, never Notion's.

That rule is worth more here than for any other provider. A Notion page's title
is not a field: it is the first `title`-typed entry in a `properties` dict whose
*key* the user chose, containing a list of rich-text runs, each with its own
`plain_text`. Two pages in the same workspace can name that property differently.
Letting that structure travel upward would put four levels of Notion's data model
into our prompts and our tests.

`Notion-Version` is mandatory
-----------------------------
Every request must carry it. Without it Notion answers `400 validation_error`,
which `BaseClient` reports as a generic failed request — a 400 that looks like a
bad query rather than a missing header. It is pinned rather than tracked: Notion
changes response shapes between versions, so "latest" would mean this client
breaks on a date somebody else picks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.integrations.base import BaseClient

SEARCH_URL = "https://api.notion.com/v1/search"

NOTION_VERSION = "2022-06-28"
"""Pinned. See the module docstring — "latest" is an upgrade scheduled by
somebody who has never seen this code."""

MAX_RESULTS = 25
"""A page of results, bounded for the same reason as everywhere else: this feeds
a prompt eventually."""


@dataclass(frozen=True)
class NotionPage:
    """One page, reduced to what this product uses."""

    page_id: str
    title: str
    url: str | None
    last_edited_at: datetime | None


class NotionClient(BaseClient):
    """Searches the pages the user shared with the integration."""

    extra_headers = {"Notion-Version": NOTION_VERSION}

    forbidden_message = (
        "This Notion integration has not been given access to that content. "
        "Share the page with it in Notion, or reconnect the workspace."
    )

    async def search_pages(
        self, access_token: str, *, query: str = "", limit: int = MAX_RESULTS
    ) -> list[NotionPage]:
        """Pages the connected integration can see, most recently edited first.

        Notion's search is scoped to what the user explicitly shared during
        consent — so an empty query returns their picker selection rather than
        the whole workspace. That is a *feature* worth not defeating: it means
        this call cannot read a page nobody offered.
        """
        body: dict[str, Any] = {
            "page_size": min(limit, MAX_RESULTS),
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }

        if query:
            body["query"] = query

        payload = await self.post_json(SEARCH_URL, access_token=access_token, body=body)

        return [
            _as_page(item)
            for item in payload.get("results", [])
            # A search filtered to pages can still return a database whose parent
            # matched. Rendering one as a page gives a row that opens onto
            # something with no title property at all.
            if item.get("object") == "page"
        ]


def _as_page(item: dict[str, Any]) -> NotionPage:
    """Translate one Notion page object into ours."""
    edited = item.get("last_edited_time")

    return NotionPage(
        page_id=str(item.get("id", "")),
        title=_title_of(item),
        url=item.get("url"),
        last_edited_at=datetime.fromisoformat(str(edited)) if edited else None,
    )


def _title_of(item: dict[str, Any]) -> str:
    """Find a page's title, wherever the user's schema happened to put it.

    There is no `title` field. There is a `properties` dict whose keys are the
    workspace's own column names, one of which has `"type": "title"` — and the
    value is a list of rich-text runs that is *empty* for an untitled page.

    Searching by type rather than by a well-known key is what makes this work for
    a database row whose title column is called "Task" or "Nom". Looking up
    `properties["Name"]` works on a fresh workspace and fails on every real one.
    """
    properties = item.get("properties")

    if not isinstance(properties, dict):
        return "(untitled)"

    for value in properties.values():
        if not isinstance(value, dict) or value.get("type") != "title":
            continue

        runs = value.get("title", [])
        text = "".join(str(run.get("plain_text", "")) for run in runs if isinstance(run, dict))

        # An untitled page has a title property holding an empty list. "" renders
        # as a blank row nobody can identify or click.
        return text.strip() or "(untitled)"

    return "(untitled)"
