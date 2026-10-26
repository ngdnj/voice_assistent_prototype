"""Allow idempotent meeting reschedules without changing existing records.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_operations_operation_type"), "operations", type_="check")
    op.create_check_constraint(
        op.f("ck_operations_operation_type"),
        "operations",
        "operation_type IN ('create_task', 'create_meeting', 'create_reminder', 'reschedule_meeting')",
    )


def downgrade() -> None:
    # Refuse an incompatible rollback instead of deleting the history of completed actions.
    if op.get_bind().scalar(
        text("SELECT EXISTS (SELECT 1 FROM operations WHERE operation_type = 'reschedule_meeting')")
    ):
        raise RuntimeError("Cannot downgrade while reschedule operations exist")
    op.drop_constraint(op.f("ck_operations_operation_type"), "operations", type_="check")
    op.create_check_constraint(
        op.f("ck_operations_operation_type"),
        "operations",
        "operation_type IN ('create_task', 'create_meeting', 'create_reminder')",
    )
