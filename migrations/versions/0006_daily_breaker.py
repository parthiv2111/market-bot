"""Persist daily policy, irreversible stop latch and execution intents."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_controls",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("percentage", sa.Numeric(20, 10), nullable=False),
        sa.Column("e0", sa.Numeric(30, 6), nullable=False),
        sa.Column("triggered", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "percentage > 0 AND percentage <= 100 AND e0 > 0", name="ck_daily_policy"
        ),
    )
    op.create_table(
        "stop_intents",
        sa.Column(
            "account_id", sa.String(128), sa.ForeignKey("ledger_accounts.id"), primary_key=True
        ),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("target", sa.String(160), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.execute("""CREATE FUNCTION protect_daily_control() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Daily controls cannot be deleted'; END IF;
            IF OLD.account_id <> NEW.account_id OR OLD.session_date <> NEW.session_date
               OR OLD.percentage <> NEW.percentage OR OLD.e0 <> NEW.e0
               OR (OLD.triggered AND
                   (NOT NEW.triggered OR OLD.payload::jsonb <> NEW.payload::jsonb))
            THEN RAISE EXCEPTION 'Daily policy and triggered stops are immutable'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER daily_control_protection BEFORE UPDATE OR DELETE ON daily_controls
        FOR EACH ROW EXECUTE FUNCTION protect_daily_control()""")
    op.execute("""CREATE TRIGGER stop_intents_immutable BEFORE UPDATE OR DELETE ON stop_intents
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()""")


def downgrade():
    op.drop_table("stop_intents")
    op.drop_table("daily_controls")
    op.execute("DROP FUNCTION protect_daily_control()")
