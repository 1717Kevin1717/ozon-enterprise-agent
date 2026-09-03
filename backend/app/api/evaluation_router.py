from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.router import context, require_role
from app.db.session import get_session
from app.services.agent_evaluation import evaluation_summary, evaluation_view, list_evaluations, run_deterministic_suite


evaluation_router = APIRouter(prefix="/api/v1/evaluations", tags=["agent-evaluation"])


@evaluation_router.get("")
async def evaluations(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "analyst")
    return {"success": True, "data": [evaluation_view(item) for item in await list_evaluations(session, ctx["company_id"])]}


@evaluation_router.get("/summary")
async def summary(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "analyst")
    return {"success": True, "data": await evaluation_summary(session, ctx["company_id"])}


@evaluation_router.post("/run")
async def run_suite(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "analyst")
    rows = await run_deterministic_suite(session, ctx["company_id"], ctx["user_id"], ctx["role"])
    return {
        "success": True,
        "data": {
            "evaluations": [evaluation_view(item) for item in rows],
            "summary": await evaluation_summary(session, ctx["company_id"]),
            "notice": "本轮只运行确定性 Planner 回归评测，不调用 GLM，不产生模型 token 费用。",
        },
    }
