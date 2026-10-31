"""M8 工具问答入口；继续复用 M5 的有界循环。"""

from fastapi import APIRouter, Depends

from app.dependencies import get_query_service
from app.schemas.queries import AgentRequest

router = APIRouter(prefix="/api/agent", tags=["M4 工具调用"])


@router.post("/query")
async def ask_agent(body: AgentRequest, service=Depends(get_query_service)):
    return await service.agent_query(body)
