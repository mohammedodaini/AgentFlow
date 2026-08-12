"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the schema change."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Reverse the schema change.

    Autogenerate writes this for you, but read it before trusting it: a
    downgrade that drops a column is a downgrade that destroys data. For
    destructive changes the honest answer is often `raise NotImplementedError`
    plus a restore-from-backup runbook.
    """
    ${downgrades if downgrades else "pass"}
