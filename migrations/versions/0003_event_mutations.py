"""Add logical deletion and idempotent record editing.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_operations_operation_type"), "operations", type_="check")
    op.create_check_constraint(
        op.f("ck_operations_operation_type"),
        "operations",
        "operation_type IN ('create_task', 'create_meeting', 'create_reminder', "
        "'reschedule_meeting', 'update_event', 'delete_event')",
    )


def downgrade() -> None:
    used = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM operations WHERE operation_type IN ('update_event','delete_event'))"
            " OR EXISTS (SELECT 1 FROM events WHERE deleted_at IS NOT NULL)"
        )
    )
    if used:
        raise RuntimeError("Cannot downgrade while edit/delete history exists")
    op.drop_constraint(op.f("ck_operations_operation_type"), "operations", type_="check")
    op.create_check_constraint(
        op.f("ck_operations_operation_type"),
        "operations",
        "operation_type IN ('create_task', 'create_meeting', 'create_reminder', 'reschedule_meeting')",
    )
    op.drop_column("events", "deleted_at")
