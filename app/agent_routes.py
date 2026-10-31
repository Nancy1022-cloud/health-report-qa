"""M8 工具问答入口；继续复用 M5 的有界循环。"""

from fastapi import APIRouter, Depends

from app.dependencies import get_query_service
from app.schemas.queries import AgentRequest
from app.schemas.responses import AgentResponse, ERROR_RESPONSES

router = APIRouter(prefix="/api/agent", tags=["M4 工具调用"], responses=ERROR_RESPONSES)


@router.post("/query", response_model=AgentResponse)
async def ask_agent(body: AgentRequest, service=Depends(get_query_service)):
    return await service.agent_query(body)
