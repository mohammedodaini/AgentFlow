"""Data access for `conversations` and `messages`.

Layer: repositories. Takes a session, owns no transaction.

Why this exists rather than queries inside the service
------------------------------------------------------
`messages` has no `organization_id` — it reaches the tenant through
`conversations`, exactly as `document_chunks` reaches it through `documents`
(M6). Every read of a message is therefore a *join*, and a join that can be
written without its tenancy predicate is a join somebody eventually writes that
way. One module owns it.

It is also where the append-only rule is enforced by omission: there is no
`update_message` and no `delete_message`. A transcript that can be edited is not
a record of what an automated system told someone, and being that record is this
table's entire purpose.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole

MAX_HISTORY_MESSAGES = 40
"""How many past turns are loaded to build a prompt.

Two bounds guard the prompt and they are not redundant. This one bounds the
*query and the graph state* — without it a thousand-message thread means a
thousand rows fetched and serialised into checkpointed state to render a dozen.
`history_token_budget` then bounds what is actually *sent* to the model.

Count here, tokens there, because each is the natural unit of the thing it
protects: rows for a database read, tokens for a bill.
"""


class ConversationRepository:
    """Tenant-scoped reads and writes for chat threads."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, organization_id: uuid.UUID, user_id: uuid.UUID | None, title: str
    ) -> Conversation:
        conversation = Conversation(organization_id=organization_id, user_id=user_id, title=title)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def get(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        """One thread, or None if it belongs to another tenant.

        Deliberately *without* its messages. The caller that wants them asks for
        a bounded page; eager-loading here would mean every "does this thread
        exist?" check dragged an entire transcript across the wire.
        """
        conversation: Conversation | None = await self._session.scalar(
            select(Conversation).where(
                Conversation.organization_id == organization_id,
                Conversation.id == conversation_id,
            )
        )
        return conversation

    async def list_for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        *,
        include_archived: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Conversation]:
        """This person's threads, newest first.

        Scoped to the user, not merely to the organization. A chat thread is the
        most personal thing in this schema — what someone typed, in their own
        words, often about their own situation — and "everyone in the company can
        read everyone's threads" is not a default anybody consents to. An
        org-wide view is a separate, deliberate feature with its own permission
        check, not the absence of a predicate here.
        """
        query = select(Conversation).where(
            Conversation.organization_id == organization_id, Conversation.user_id == user_id
        )

        if not include_archived:
            query = query.where(Conversation.archived_at.is_(None))

        return list(
            await self._session.scalars(
                query.order_by(Conversation.created_at.desc(), Conversation.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    async def count_for_user(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        *,
        include_archived: bool = False,
    ) -> int:
        query = select(func.count(Conversation.id)).where(
            Conversation.organization_id == organization_id, Conversation.user_id == user_id
        )

        if not include_archived:
            query = query.where(Conversation.archived_at.is_(None))

        return await self._session.scalar(query) or 0

    async def add_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        agent_run_id: uuid.UUID | None = None,
        token_usage: dict[str, Any] | None = None,
    ) -> Message:
        """Append one turn. There is no counterpart that edits or removes one."""
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            agent_run_id=agent_run_id,
            token_usage=token_usage,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def recent_messages(
        self, conversation_id: uuid.UUID, *, limit: int = MAX_HISTORY_MESSAGES
    ) -> list[Message]:
        """The last `limit` turns, returned oldest-first.

        The ordering is the subtle part. "The most recent N" requires `ORDER BY
        created_at DESC LIMIT N` — otherwise the database returns the *first* N
        turns of a long thread and the model is handed the beginning of a
        conversation as though it were the end. The list is then reversed in
        Python, because reading order is what a prompt needs.

        Reversing in SQL would take a subquery for no gain: `limit` is capped at
        forty rows, and reversing forty items in memory is free.
        """
        messages = list(
            await self._session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        )
        messages.reverse()
        return messages

    async def count_messages(self, conversation_id: uuid.UUID) -> int:
        total = await self._session.scalar(
            select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
        )
        return total or 0
