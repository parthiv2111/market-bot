"""Initial instruments, daily candles, ingestion provenance and policy records."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("exchange", "symbol", name="uq_instruments_exchange_symbol"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name="ck_ingestion_status"
        ),
    )
    op.create_table(
        "candles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("session_date", sa.Date, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("open", sa.Numeric(20, 6), nullable=False),
        sa.Column("high", sa.Numeric(20, 6), nullable=False),
        sa.Column("low", sa.Numeric(20, 6), nullable=False),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
        sa.Column("volume", sa.Numeric(24, 0), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingestion_run_id", sa.String(36), sa.ForeignKey("ingestion_runs.id"), nullable=False
        ),
        sa.UniqueConstraint(
            "instrument_id", "session_date", "provider", name="uq_candles_identity"
        ),
        sa.CheckConstraint(
            "low > 0 AND high >= low AND open >= low AND open <= high "
            "AND close >= low AND close <= high AND volume >= 0",
            name="ck_candles_ohlcv",
        ),
    )
    op.create_table(
        "policy_versions",
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    for table in ["policy_versions", "candles", "ingestion_runs", "instruments"]:
        op.drop_table(table)
