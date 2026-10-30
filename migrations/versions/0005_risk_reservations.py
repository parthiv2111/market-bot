"""Shared portfolio capacity and terminal-confirmed reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "risk_capacity",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "risk_reservations",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("reservation_id", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("terminal_evidence", sa.JSON()),
        sa.CheckConstraint("status IN ('active', 'released')", name="ck_reservation_status"),
    )


def downgrade():
    op.drop_table("risk_reservations")
    op.drop_table("risk_capacity")
