"""Recovery queue for durable scheduled Operations Agent claims."""

from sqlalchemy import select, update

from backend.database import AsyncSessionLocal
from backend.models.operations_agent import OperationsAgentRun
from backend.services.operations_agent_runtime_service import schedule_operations_agent_run


async def list_queued_scheduled_run_ids(limit: int = 200) -> list[str]:
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(OperationsAgentRun.id)
            .where(
                OperationsAgentRun.trigger_type == "scheduled",
                OperationsAgentRun.status == "queued",
            )
            .order_by(OperationsAgentRun.created_at)
            .limit(limit)
        )
        return list(rows)


async def recover_queued_scheduled_runs_local() -> list[str]:
    run_ids = await list_queued_scheduled_run_ids()
    for run_id in run_ids:
        schedule_operations_agent_run(run_id)
    return run_ids


async def recover_operations_agent_runs_on_startup() -> None:
    """Preserve queued scheduled claims; fail only work that cannot be resumed."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(OperationsAgentRun)
            .where(OperationsAgentRun.status == "running")
            .values(
                status="failed",
                error_message="Operations Agent run interrupted by server restart",
            )
        )
        await session.execute(
            update(OperationsAgentRun)
            .where(
                OperationsAgentRun.status == "queued",
                OperationsAgentRun.trigger_type != "scheduled",
            )
            .values(
                status="failed",
                error_message="Operations Agent run interrupted by server restart",
            )
        )
        await session.commit()
