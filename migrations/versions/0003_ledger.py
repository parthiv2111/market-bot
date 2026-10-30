"""Append-only portfolio event journal."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ledger_accounts", sa.Column("id", sa.String(128), primary_key=True))
    op.create_table(
        "ledger_events",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "sequence", name="uq_ledger_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_ledger_sequence"),
    )
    op.execute("""CREATE FUNCTION reject_ledger_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Ledger events are immutable'; END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER ledger_events_immutable BEFORE UPDATE OR DELETE
        ON ledger_events FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()""")


def downgrade():
    op.drop_table("ledger_events")
    op.execute("DROP FUNCTION reject_ledger_mutation()")
    op.drop_table("ledger_accounts")
