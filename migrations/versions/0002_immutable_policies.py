"""Protect stored policy history against updates and deletes."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION reject_policy_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Policy versions are immutable';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER policy_versions_immutable
        BEFORE UPDATE OR DELETE ON policy_versions
        FOR EACH ROW EXECUTE FUNCTION reject_policy_mutation()
    """)


def downgrade():
    op.execute("DROP TRIGGER policy_versions_immutable ON policy_versions")
    op.execute("DROP FUNCTION reject_policy_mutation()")
