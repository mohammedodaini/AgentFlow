"""Schema shape of the identity/tenancy tables (M2).

These assertions read SQLAlchemy's in-memory metadata — no database required.
That is deliberate: the schema is a contract, and a contract test that needs a
running server is a contract test people skip.

What they protect is the stuff that is silently expensive to get wrong:
constraint *names* (Alembic autogenerate produces unreadable diffs without a
naming convention), the tenancy join, and the uniqueness rules that stop
duplicate accounts and duplicate memberships.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from app.models import Base, Membership, Organization, Role, User


def test_table_names_are_plural_snake_case() -> None:
    """Convention from docs/database.md — enforced, not merely written down."""
    assert Organization.__tablename__ == "organizations"
    assert User.__tablename__ == "users"
    assert Membership.__tablename__ == "memberships"


def test_every_table_has_uuid_pk_and_timestamps() -> None:
    """The three columns that must exist everywhere, on every future table too."""
    for model in (Organization, User, Membership):
        columns = model.__table__.columns

        assert columns["id"].primary_key, model.__tablename__
        assert columns["created_at"].server_default is not None, model.__tablename__
        assert columns["updated_at"].server_default is not None, model.__tablename__


def test_primary_keys_default_to_uuid7() -> None:
    """Ids are generated in Python, so we know them before INSERT."""
    default = Organization.__table__.columns["id"].default

    assert default is not None
    assert default.arg(None).version == 7


def test_naming_convention_produces_readable_constraint_names() -> None:
    """Without this, Postgres invents names and migrations become unreviewable.

    A generated diff saying `DROP CONSTRAINT organizations_slug_key` is noise;
    `uq_organizations_slug` says what it protects.
    """
    assert Organization.__table__.primary_key.name == "pk_organizations"
    assert {index.name for index in Organization.__table__.indexes} == {"ix_organizations_slug"}
    assert {
        constraint.name
        for constraint in Membership.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    } == {"fk_memberships_user_id_users", "fk_memberships_organization_id_organizations"}


def test_user_email_is_unique_and_indexed() -> None:
    """Login looks users up by email on every request — unindexed is a scan."""
    email = User.__table__.columns["email"]

    assert email.unique is True
    assert email.index is True
    assert email.nullable is False


def test_membership_is_unique_per_user_and_organization() -> None:
    """One person, one row per org. The DB enforces it; application code cannot.

    Two concurrent invite requests both pass an application-level "already a
    member?" check and both insert. Only a unique constraint stops that.
    """
    unique = next(
        constraint
        for constraint in Membership.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    )

    assert unique.name == "uq_memberships_user_id"
    assert [column.name for column in unique.columns] == ["user_id", "organization_id"]


def test_membership_foreign_keys_cascade_to_both_parents() -> None:
    """Deleting a user or an org must not leave orphaned membership rows."""
    table = Membership.__table__

    for column_name, target in (("user_id", "users.id"), ("organization_id", "organizations.id")):
        foreign_key = next(iter(table.columns[column_name].foreign_keys))

        assert foreign_key.target_fullname == target
        assert foreign_key.ondelete == "CASCADE"


def test_membership_has_no_index_redundant_with_its_unique_constraint() -> None:
    """`uq_memberships_user_id` covers (user_id, organization_id).

    Postgres can serve a `user_id`-only lookup from that composite index, so a
    standalone index on `user_id` would cost storage and write throughput while
    buying nothing. `organization_id` is the trailing column, so it does need
    its own index — asserting both directions keeps the reasoning honest.
    """
    index_names = {index.name for index in Membership.__table__.indexes}

    assert index_names == {"ix_memberships_organization_id"}


def test_role_values_are_the_documented_three() -> None:
    """`owner | admin | member` per docs/database.md."""
    assert [role.value for role in Role] == ["owner", "admin", "member"]
    assert Role.OWNER == "owner"  # StrEnum compares equal to its wire value


def test_all_models_share_one_metadata() -> None:
    """Alembic autogenerate reads exactly one MetaData — everything must be in it."""
    assert {"organizations", "users", "memberships"} <= set(Base.metadata.tables)
