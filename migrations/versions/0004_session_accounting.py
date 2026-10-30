"""Durable session observations and baseline projections."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "session_snapshots",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("observation_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("observation", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "sequence", name="uq_session_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_session_sequence"),
    )
    op.execute("""CREATE TRIGGER session_snapshots_immutable BEFORE UPDATE OR DELETE
        ON session_snapshots FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()""")


def downgrade():
    op.drop_table("session_snapshots")
