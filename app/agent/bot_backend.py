"""Connect the MAX transport port to the transport-independent agent."""

from app.agent.agent import Assistant
from app.bot.service import BackendRequest


class AgentBackend:
    def __init__(self, assistant: Assistant) -> None:
        self.assistant = assistant

    async def answer(self, request: BackendRequest) -> str:
        reply = await self.assistant.handle(
            request.owner_id,
            request.text,
            chat_id=request.chat_id,
            reference_time=request.reference_time,
            timezone=request.timezone,
            operation_id=request.job_id,
            request_id=str(request.request_id),
        )
        return reply.text
