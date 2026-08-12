"""Organization scoping and member management over HTTP (M3).

This file is where multi-tenancy is actually proved. Every later milestone
filters its queries by `membership.organization_id`, and that value comes from
the `X-Organization-Id` header resolved in `app/auth/dependencies.py` — so if
the boundary leaks, every feature built on top of it leaks with it.

The privilege-escalation cases matter as much as the happy paths. "Admin" and
"owner" are only different roles if an admin genuinely cannot make themselves
one.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from httpx import AsyncClient

from tests.e2e.test_auth import PASSWORD, auth, register

ORGS_URL = "/api/v1/organizations"


async def sign_up(client: AsyncClient, email: str) -> dict[str, Any]:
    """Register someone and return their tokens plus their personal org id."""
    tokens = await register(client, email=email, password=PASSWORD)
    listing = await client.get(ORGS_URL, headers=auth(tokens))
    organization = listing.json()[0]["organization"]
    return {"tokens": tokens, "organization_id": organization["id"], "email": email}


def scoped(account: dict[str, Any], organization_id: str | None = None) -> dict[str, str]:
    """Authorization plus the tenancy header — what a real client sends."""
    return {
        **auth(account["tokens"]),
        "X-Organization-Id": organization_id or account["organization_id"],
    }


def members_url(organization_id: str) -> str:
    return f"{ORGS_URL}/{organization_id}/members"


# --------------------------------------------------------------------------
# Creating and listing
# --------------------------------------------------------------------------


async def test_creating_an_organization_makes_the_caller_its_owner(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")

    response = await client.post(
        ORGS_URL, json={"name": "Acme Corp"}, headers=auth(owner["tokens"])
    )

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["slug"] == "acme-corp"
    assert body["plan"] == "free"

    listing = await client.get(ORGS_URL, headers=auth(owner["tokens"]))
    roles = {item["organization"]["slug"]: item["role"] for item in listing.json()}
    assert roles["acme-corp"] == "owner"


async def test_a_taken_slug_is_deduplicated(client: AsyncClient) -> None:
    """Two companies may share a name; a slug is unique by construction."""
    owner = await sign_up(client, "ada@example.com")
    headers = auth(owner["tokens"])

    first = await client.post(ORGS_URL, json={"name": "Acme"}, headers=headers)
    second = await client.post(ORGS_URL, json={"name": "Acme"}, headers=headers)

    assert first.json()["slug"] == "acme"
    assert second.json()["slug"] == "acme-2"


async def test_creating_an_organization_needs_no_tenancy_header(client: AsyncClient) -> None:
    """Chicken and egg: you cannot scope a request to an org that does not exist."""
    owner = await sign_up(client, "ada@example.com")

    response = await client.post(ORGS_URL, json={"name": "Acme"}, headers=auth(owner["tokens"]))

    assert response.status_code == HTTPStatus.CREATED


# --------------------------------------------------------------------------
# The tenancy boundary
# --------------------------------------------------------------------------


async def test_the_organization_header_is_required(client: AsyncClient) -> None:
    """422, not a silently-chosen default.

    A default organization would mean a client that forgot the header quietly
    operates on the wrong tenant — the worst failure mode available here,
    because it succeeds.
    """
    owner = await sign_up(client, "ada@example.com")

    response = await client.get(
        members_url(owner["organization_id"]), headers=auth(owner["tokens"])
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_a_stranger_cannot_read_another_organization(client: AsyncClient) -> None:
    """The core isolation guarantee — and it answers 404 rather than 403.

    403 would confirm the organization exists, turning the endpoint into an
    oracle for enumerating tenant ids.
    """
    owner = await sign_up(client, "ada@example.com")
    stranger = await sign_up(client, "eve@example.com")

    response = await client.get(
        members_url(owner["organization_id"]),
        headers=scoped(stranger, owner["organization_id"]),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_a_header_that_disagrees_with_the_path_is_refused(client: AsyncClient) -> None:
    """Authorize against one org, act on another — the classic mixed-source bug."""
    attacker = await sign_up(client, "eve@example.com")
    victim = await sign_up(client, "ada@example.com")

    response = await client.get(
        members_url(victim["organization_id"]),
        headers=scoped(attacker),  # header names Eve's org, path names Ada's
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_a_member_can_read_the_roster(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")

    response = await client.get(members_url(owner["organization_id"]), headers=scoped(owner))

    assert response.status_code == HTTPStatus.OK
    members = response.json()
    assert len(members) == 1
    assert members[0]["email"] == "ada@example.com"
    assert members[0]["role"] == "owner"


# --------------------------------------------------------------------------
# Adding members
# --------------------------------------------------------------------------


async def test_an_owner_can_add_a_member(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")
    await sign_up(client, "grace@example.com")

    response = await client.post(
        members_url(owner["organization_id"]),
        json={"email": "grace@example.com"},
        headers=scoped(owner),
    )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["role"] == "member"


async def test_inviting_an_unknown_email_is_404(client: AsyncClient) -> None:
    """M3 adds existing accounts only — inviting strangers needs its own flow."""
    owner = await sign_up(client, "ada@example.com")

    response = await client.post(
        members_url(owner["organization_id"]),
        json={"email": "nobody@example.com"},
        headers=scoped(owner),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_adding_the_same_person_twice_is_409(client: AsyncClient) -> None:
    """Backed by the UNIQUE constraint from M2; checked here for a clean error."""
    owner = await sign_up(client, "ada@example.com")
    await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))
    again = await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))

    assert again.status_code == HTTPStatus.CONFLICT


async def test_an_ordinary_member_cannot_add_anyone(client: AsyncClient) -> None:
    """Reading the roster and changing it are different privileges."""
    owner = await sign_up(client, "ada@example.com")
    member = await sign_up(client, "grace@example.com")
    await sign_up(client, "alan@example.com")
    url = members_url(owner["organization_id"])

    await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))

    response = await client.post(
        url,
        json={"email": "alan@example.com"},
        headers=scoped(member, owner["organization_id"]),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


# --------------------------------------------------------------------------
# Roles and privilege escalation
# --------------------------------------------------------------------------


async def test_an_owner_can_promote_a_member_to_admin(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")
    await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))
    member_id = added.json()["user_id"]

    response = await client.patch(
        f"{url}/{member_id}", json={"role": "admin"}, headers=scoped(owner)
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["role"] == "admin"


async def test_an_admin_cannot_grant_ownership(client: AsyncClient) -> None:
    """Otherwise admin and owner are the same role with different names.

    An admin who can hand out ownership can hand it to themselves, then remove
    the real owner.
    """
    owner = await sign_up(client, "ada@example.com")
    admin = await sign_up(client, "grace@example.com")
    await sign_up(client, "alan@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(
        url, json={"email": "grace@example.com", "role": "admin"}, headers=scoped(owner)
    )
    admin_id = added.json()["user_id"]

    response = await client.patch(
        f"{url}/{admin_id}",
        json={"role": "owner"},
        headers=scoped(admin, owner["organization_id"]),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_an_admin_cannot_demote_an_owner(client: AsyncClient) -> None:
    """The same attack from the other direction: demote, then promote yourself."""
    owner = await sign_up(client, "ada@example.com")
    admin = await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    await client.post(
        url, json={"email": "grace@example.com", "role": "admin"}, headers=scoped(owner)
    )
    owner_id = (await client.get(url, headers=scoped(owner))).json()[0]["user_id"]

    response = await client.patch(
        f"{url}/{owner_id}",
        json={"role": "member"},
        headers=scoped(admin, owner["organization_id"]),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


async def test_the_last_owner_cannot_be_demoted(client: AsyncClient) -> None:
    """An ownerless organization cannot be billed, renamed, or given a new owner."""
    owner = await sign_up(client, "ada@example.com")
    url = members_url(owner["organization_id"])
    owner_id = (await client.get(url, headers=scoped(owner))).json()[0]["user_id"]

    response = await client.patch(
        f"{url}/{owner_id}", json={"role": "member"}, headers=scoped(owner)
    )

    assert response.status_code == HTTPStatus.CONFLICT


async def test_ownership_can_be_handed_over_when_there_are_two_owners(client: AsyncClient) -> None:
    """The rule is "keep at least one owner", not "the first owner is forever"."""
    owner = await sign_up(client, "ada@example.com")
    await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(
        url, json={"email": "grace@example.com", "role": "owner"}, headers=scoped(owner)
    )
    assert added.status_code == HTTPStatus.CREATED

    owner_id = (await client.get(url, headers=scoped(owner))).json()[0]["user_id"]
    response = await client.patch(
        f"{url}/{owner_id}", json={"role": "admin"}, headers=scoped(owner)
    )

    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


async def test_an_owner_can_remove_a_member(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")
    await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))
    member_id = added.json()["user_id"]

    response = await client.delete(f"{url}/{member_id}", headers=scoped(owner))

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert len((await client.get(url, headers=scoped(owner))).json()) == 1


async def test_a_member_can_remove_themselves(client: AsyncClient) -> None:
    """Leaving is not a privileged action — same endpoint, different rules."""
    owner = await sign_up(client, "ada@example.com")
    member = await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))
    member_id = added.json()["user_id"]

    response = await client.delete(
        f"{url}/{member_id}", headers=scoped(member, owner["organization_id"])
    )

    assert response.status_code == HTTPStatus.NO_CONTENT


async def test_the_last_owner_cannot_leave(client: AsyncClient) -> None:
    owner = await sign_up(client, "ada@example.com")
    url = members_url(owner["organization_id"])
    owner_id = (await client.get(url, headers=scoped(owner))).json()[0]["user_id"]

    response = await client.delete(f"{url}/{owner_id}", headers=scoped(owner))

    assert response.status_code == HTTPStatus.CONFLICT


async def test_removing_a_member_leaves_the_user_account_intact(client: AsyncClient) -> None:
    """Membership is a seat, not the person. They still own their personal org."""
    owner = await sign_up(client, "ada@example.com")
    member = await sign_up(client, "grace@example.com")
    url = members_url(owner["organization_id"])

    added = await client.post(url, json={"email": "grace@example.com"}, headers=scoped(owner))
    await client.delete(f"{url}/{added.json()['user_id']}", headers=scoped(owner))

    still_there = await client.get("/api/v1/users/me", headers=auth(member["tokens"]))
    assert still_there.status_code == HTTPStatus.OK
