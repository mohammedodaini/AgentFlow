"""Conversation business logic: threads, turns, and the agent run between them.

Layer: services. Owns no transaction — `app/db/deps.py` commits — and, like
`DocumentService`, **does not enqueue**. That looks like an omission and is
ADR-0008: the memory-extraction worker reads the conversation back out of
Postgres, so a job delivered before the commit would find the thread without the
two messages it exists to learn from, and would succeed having learned nothing.
The route commits, then enqueues.

The order inside `send()` is the design
---------------------------------------
1. **Load the history first, before the new turn is written.** The question is
   passed to the agent as `question`; the history is what came *before* it.
   Appending first and then reading history would hand the model the question
   twice — once as context and once as the thing to answer — and a model shown
   its own question as history tends to answer the older copy.
2. **Append the user's message, then run the agent.** `AgentService` commits its
   run row before the graph starts (M9), which carries the user's message with
   it. That is the behaviour worth having: what someone typed is durable before
   any model is called, so a crash mid-generation loses the answer and never the
   question.
3. **Append the assistant's reply, linked to the run.** `messages.agent_run_id`
   is the bridge from a sentence in a chat window to the trace that produced it.
4. **Record the extraction task**, and let the route enqueue it after the commit.

What is deliberately not here
-----------------------------
No streaming. `POST /ask/stream` (M7) streams because a single answer benefits
from first-token latency; a conversation turn also has to *persist* the reply,
and a stream that fails halfway has already shown the client something it can no
longer commit. Making that correct means writing the message from the stream's
completion callback — real work with real failure modes. M13 builds the chat UI,
and that is when it earns its complexity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.history import HistoryTurn
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.llm.base import LLMProvider
from app.models.agent_run import AgentRun
from app.models.conversation import TITLE_MAX_LENGTH, Conversation
from app.models.message import Message, MessageRole
from app.models.task import EXTRACT_MEMORIES, Task, TaskStatus
from app.rag.embeddings import EmbeddingProvider
from app.repositories.conversation_repository import MAX_HISTORY_MESSAGES, ConversationRepository
from app.services.agent_service import AgentService

logger = structlog.get_logger(__name__)

DEFAULT_TITLE = "New conversation"
"""What a thread is called before anyone has said anything in it."""


@dataclass(frozen=True)
class SentTurn:
    """Everything one exchange produced.

    Returned as a bundle rather than only the reply, because the route needs the
    task row to enqueue after committing and the run id to link the answer to its
    trace. A method returning just the assistant message would send the route
    looking for the rest.
    """

    conversation: Conversation
    user_message: Message
    assistant_message: Message
    run: AgentRun
    task: Task


class ConversationService:
    """Threads, turns, and the agent run that joins them."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingProvider,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._agents = AgentService(session, embedder, llm, settings)
        self._settings = settings

    async def create(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None, *, title: str | None = None
    ) -> Conversation:
        """Open an empty thread."""
        return await self._conversations.create(
            organization_id=organization_id, user_id=user_id, title=_clip(title or DEFAULT_TITLE)
        )

    async def list_for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        *,
        include_archived: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Conversation], int]:
        """A page of this person's threads, and the total for the envelope."""
        return (
            await self._conversations.list_for_user(
                organization_id,
                user_id,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            ),
            await self._conversations.count_for_user(
                organization_id, user_id, include_archived=include_archived
            ),
        )

    async def get(self, organization_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
        """One thread, or `NotFoundError` if it is another tenant's.

        404 rather than 403 for a thread that exists elsewhere — the same rule as
        every other resource here. A distinct status turns the endpoint into an
        oracle for enumerating ids across organizations.
        """
        conversation = await self._conversations.get(organization_id, conversation_id)

        if conversation is None:
            message = "Conversation not found."
            raise NotFoundError(message)

        return conversation

    async def messages(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, *, limit: int = 50
    ) -> list[Message]:
        """The transcript, oldest first.

        Goes through `get()` first, so a request for another tenant's thread is a
        404 rather than an empty list. An empty list is a subtler leak than it
        looks: it says "this id exists and is empty", which is one bit more than
        a stranger should learn.
        """
        await self.get(organization_id, conversation_id)
        return await self._conversations.recent_messages(conversation_id, limit=limit)

    async def send(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        conversation_id: uuid.UUID,
        content: str,
    ) -> SentTurn:
        """Append a question, answer it through the agent, append the reply."""
        conversation = await self.get(organization_id, conversation_id)

        # Read before writing. See the module docstring: the history is what
        # preceded this question, and the question is not part of it.
        history = [
            HistoryTurn(role=message.role.value, content=message.content)
            for message in await self._conversations.recent_messages(
                conversation_id, limit=MAX_HISTORY_MESSAGES
            )
        ]

        user_message = await self._conversations.add_message(
            conversation_id=conversation_id, role=MessageRole.USER, content=content
        )

        if conversation.title == DEFAULT_TITLE:
            # Derived from the first thing said, by code. `docs/agents.md`: if
            # code can do it, code does it — and an LLM-titled thread would add a
            # model call, its latency and its failure mode to the act of sending
            # a first message.
            conversation.title = _clip(content)

        run = await self._agents.run_rag_agent(
            organization_id,
            content,
            user_id=user_id,
            conversation_id=conversation_id,
            history=history,
        )

        output = run.output or {}
        assistant_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=str(output.get("answer", "")),
            agent_run_id=run.id,
            # Denormalised onto the turn, so a thread's cost survives the run row
            # being pruned. See `Message.token_usage`.
            token_usage={"total": run.total_tokens},
        )

        task = Task(
            organization_id=organization_id,
            kind=EXTRACT_MEMORIES,
            payload={
                "conversation_id": str(conversation_id),
                "agent_run_id": str(run.id),
                "user_id": str(user_id) if user_id else None,
            },
            status=TaskStatus.QUEUED,
        )
        self._session.add(task)
        await self._session.flush()

        logger.info(
            "conversation.turn",
            conversation_id=str(conversation_id),
            run_id=str(run.id),
            history_turns=len(history),
            total_tokens=run.total_tokens,
        )

        return SentTurn(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            run=run,
            task=task,
        )


def _clip(text: str) -> str:
    """A thread title: the first line, bounded to the column.

    First *line*, not first sentence — someone pasting three paragraphs gets a
    title from the opening line rather than a title containing three paragraphs
    minus the last two characters. The ellipsis is a real character counted
    against the limit, because a title one character over the column width is
    still a failed insert.
    """
    stripped = text.strip()
    first_line = stripped.splitlines()[0].strip() if stripped else DEFAULT_TITLE

    if len(first_line) <= TITLE_MAX_LENGTH:
        return first_line or DEFAULT_TITLE

    return first_line[: TITLE_MAX_LENGTH - 1].rstrip() + "…"
