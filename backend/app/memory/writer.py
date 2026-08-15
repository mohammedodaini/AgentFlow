"""Turning a finished conversation into durable facts.

Layer: memory. The write half; `recall.py` is the read half. Runs **after** the
response has been sent, in the arq worker — `docs/agents.md` rule 5: memory
extraction must never add latency to a user's turn. That is not a performance
preference. Extraction is a second model call whose output the user is not
waiting for, and putting it on the request path would roughly double the latency
of every message to produce something nobody is looking at yet.

The model chooses the facts. It never chooses the scope.
--------------------------------------------------------
Everything extracted here is written `scope=user`, attributed to the person
whose conversation it came from. Org-scoped memories exist in the schema and
nothing in M10 writes one.

That is ADR-0012's rule applied to a different boundary. There, the tenant is
closed over so a prompt-injected document cannot make the model search another
organization's files. Here, the scope is fixed in code so no phrasing — injected
or merely unlucky — can promote a fact from one person's private thread into
something every colleague's answers draw on. **A privacy boundary is not a field
for a model to fill in.** Promotion to org scope should be a deliberate human
act, and until there is an interface for that, there is no promotion.

A run with no user is therefore not extracted from at all: `scope=user` requires
a `user_id` (there is a check constraint), and quietly widening to org scope to
make the write succeed is exactly the failure this rule exists to prevent.

Deduplication happens twice, deliberately
-----------------------------------------
The database enforces exact uniqueness on a hash of the normalised content — a
guarantee, and one no code path can forget. This module *additionally* skips
near-duplicates by vector similarity and reinforces the memory already stored.
That is a policy: it can be wrong, it is tuned by a constant, and it catches
"Invoices are approved by Finance" against "Finance approves invoices", which no
hash ever will.

Both are needed. Without the constraint, one bug here fills the table with
copies. Without the similarity pass, every rephrasing is a new row and recall
spends its whole budget re-reading one fact.
"""

from __future__ import annotations

import enum
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.history import HistoryTurn, select_history
from app.llm.base import LLMError, LLMProvider
from app.memory.policies import reinforce
from app.models.memory import DEFAULT_IMPORTANCE, MAX_MEMORY_CHARS, Memory, MemoryScope
from app.prompts import loader as prompts
from app.rag.embeddings import EmbeddingProvider
from app.repositories.memory_repository import MemoryRepository

logger = structlog.get_logger(__name__)

EXTRACTION_SYSTEM_PROMPT = "memory/system"
EXTRACTION_PROMPT = "memory/extract"

NO_FACTS = "NONE"
"""What the model says when there is nothing worth remembering.

An explicit token rather than an empty reply, because "the model produced
nothing" and "the model failed" are different events and an empty string cannot
tell them apart.
"""

FACT_PREFIX = "- "
"""Every extracted fact begins with this, and lines that do not are discarded.

Strict parsing is the entire safety mechanism on this path. A model that ignores
the format and writes a paragraph produces *zero* memories rather than one
enormous malformed one — and the offline provider, which cannot follow this
instruction at all, therefore stores nothing rather than storing rubbish.
"""

MAX_FACTS = 5
"""Ceiling per run, matching the prompt's own instruction.

Enforced here as well as asked for there, because a prompt is a request and this
is a bound. A model returning forty "facts" from one exchange has misunderstood
the task, and the right response is to take the first few rather than to write
forty rows.
"""

NEAR_DUPLICATE_SCORE = 0.92
"""Cosine similarity above which a new fact is treated as one already stored.

High, deliberately. A false positive silently discards a genuinely new fact,
which is unrecoverable; a false negative costs one duplicate row that the recall
blend will mostly hide. When two errors differ in cost, the threshold belongs
near the cheaper one.

Measured against nothing — a starting point, in the sense M8 taught. It also
behaves differently per embedder: with the offline hashing embedder it catches
word-level rephrasing only, because that is all that embedder can see.
"""

TRANSCRIPT_BUDGET_TOKENS = 3000
"""How much of a conversation the extractor is shown.

Bounded for the same reason the prompt history is, and by the same function — so
a thread that has grown for a year costs no more to extract from than a fresh
one. Taking the most recent turns is right here too: what someone said in this
session is what has not been extracted yet.
"""

_WHITESPACE = re.compile(r"\s+")


class StoreOutcome(enum.StrEnum):
    """What happened to one candidate fact.

    An enum rather than the `bool | None` this started as. Three outcomes do not
    fit in a boolean, and `None` meaning "exact duplicate" is the kind of
    encoding that reads fine while being written and wrongly six months later.
    """

    STORED = "stored"
    EXACT_DUPLICATE = "exact_duplicate"
    REINFORCED = "reinforced"


@dataclass(frozen=True)
class ExtractionResult:
    """What one extraction pass did, in numbers.

    Returned rather than only logged, so the arq task can write it to
    `tasks.result` and a human can later ask what the background actually did —
    the same reason `tasks` mirrors the queue at all (M5).
    """

    stored: int
    duplicates: int
    reinforced: int

    @property
    def considered(self) -> int:
        return self.stored + self.duplicates


def normalize(content: str) -> str:
    """The canonical form a memory is stored as.

    Collapses whitespace and strips a trailing full stop. Casing is preserved —
    "Finance" reads better in a prompt than "finance" — and lowercasing happens
    in `content_hash` alone, so punctuation and capitalisation cannot smuggle a
    duplicate past the unique constraint while the stored text stays readable.
    """
    return _WHITESPACE.sub(" ", content).strip().rstrip(".")


def content_hash(content: str) -> str:
    """SHA-256 of the normalised, lowercased fact."""
    return hashlib.sha256(normalize(content).lower().encode("utf-8")).hexdigest()


def parse_facts(text: str) -> list[str]:
    """Pull `- fact` lines out of a model reply.

    Everything else is discarded — a preamble the model was told not to write,
    and the whole reply when it followed no format at all. Being strict is what
    makes this safe to run unattended: a confused model produces an empty list,
    not a database of nonsense.

    Facts longer than the column are dropped rather than truncated. A truncated
    fact is not a shorter fact; it is a sentence missing its qualifying clause,
    which is how "reimbursed at 45p per mile, up to 10,000 miles" becomes a
    memory that asserts something false.
    """
    if text.strip() == NO_FACTS:
        return []

    facts: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped.startswith(FACT_PREFIX):
            continue

        fact = normalize(stripped.removeprefix(FACT_PREFIX))

        if fact and len(fact) <= MAX_MEMORY_CHARS:
            facts.append(fact)

    return facts[:MAX_FACTS]


def render_transcript(turns: list[HistoryTurn]) -> str:
    """The conversation as the extractor sees it, bounded in tokens."""
    return select_history(turns, budget=TRANSCRIPT_BUDGET_TOKENS).text


class MemoryWriter:
    """Extracts durable facts from a conversation and stores them."""

    def __init__(
        self, session: AsyncSession, embedder: EmbeddingProvider, llm: LLMProvider
    ) -> None:
        self._session = session
        self._memories = MemoryRepository(session)
        self._embedder = embedder
        self._llm = llm

    async def extract_and_store(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        turns: list[HistoryTurn],
        *,
        source_run_id: uuid.UUID | None = None,
    ) -> ExtractionResult:
        """Read a conversation, write what is worth keeping.

        Returns rather than raises on a model failure. The caller is a background
        task whose real job already succeeded — the user has their answer — so a
        failed extraction should be recorded and forgotten, not retried until arq
        gives up. Retrying a model that just declined to follow a format rarely
        produces a different format.
        """
        if user_id is None:
            # No user, no `scope=user` memory — and no widening to org scope to
            # make the insert succeed. See the module docstring: "the constraint
            # would have failed" is not a reason to cross a privacy boundary.
            logger.debug("memory.extraction_skipped", reason="no_user")
            return ExtractionResult(stored=0, duplicates=0, reinforced=0)

        transcript = render_transcript(turns)

        if not transcript.strip():
            return ExtractionResult(stored=0, duplicates=0, reinforced=0)

        try:
            completion = await self._llm.complete(
                system=prompts.load_prompt(EXTRACTION_SYSTEM_PROMPT),
                prompt=prompts.render(EXTRACTION_PROMPT, transcript=transcript),
            )
        except LLMError as error:
            logger.warning("memory.extraction_failed", reason=error.code)
            return ExtractionResult(stored=0, duplicates=0, reinforced=0)

        facts = parse_facts(completion.text)

        if not facts:
            logger.debug("memory.no_facts", organization_id=str(organization_id))
            return ExtractionResult(stored=0, duplicates=0, reinforced=0)

        # Embedded in one call rather than one per fact — the same batching rule
        # as ingestion (M6). Five round trips to save one is five times the
        # latency for a job nobody waits on but which still holds a worker slot.
        embeddings = await self._embedder.embed_documents(facts)

        outcomes = [
            await self._store_one(organization_id, user_id, fact, embedding, source_run_id)
            for fact, embedding in zip(facts, embeddings, strict=True)
        ]

        result = ExtractionResult(
            stored=outcomes.count(StoreOutcome.STORED),
            duplicates=len(outcomes) - outcomes.count(StoreOutcome.STORED),
            reinforced=outcomes.count(StoreOutcome.REINFORCED),
        )

        logger.info(
            "memory.extracted",
            organization_id=str(organization_id),
            stored=result.stored,
            duplicates=result.duplicates,
            reinforced=result.reinforced,
        )
        return result

    async def _store_one(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        fact: str,
        embedding: list[float],
        source_run_id: uuid.UUID | None,
    ) -> StoreOutcome:
        """Store one fact, or reinforce whatever it duplicates."""
        digest = content_hash(fact)
        existing = await self._memories.find_by_hash(
            organization_id, scope=MemoryScope.USER, user_id=user_id, content_hash=digest
        )

        if existing is not None:
            # An exact repeat, and deliberately *not* reinforced: importance
            # tracks use, and the same conversation being extracted twice — an
            # arq retry, say — is not evidence that anybody found the fact
            # useful. Reinforcing here would let a retry storm inflate
            # importance without a single human involved.
            return StoreOutcome.EXACT_DUPLICATE

        neighbours = await self._memories.nearest(
            organization_id, embedding, user_id=user_id, limit=1
        )

        if neighbours and neighbours[0][1] >= NEAR_DUPLICATE_SCORE:
            neighbour = neighbours[0][0]
            # Said again in different words. That *is* evidence — the person
            # repeated themselves — so this reinforces rather than dropping the
            # fact silently, and the store gains a signal instead of a row.
            await self._memories.touch({neighbour.id: reinforce(neighbour.importance)})
            return StoreOutcome.REINFORCED

        await self._memories.add(
            Memory(
                organization_id=organization_id,
                scope=MemoryScope.USER,
                user_id=user_id,
                content=fact,
                content_hash=digest,
                embedding=embedding,
                importance=DEFAULT_IMPORTANCE,
                last_accessed_at=datetime.now(UTC),
                source_run_id=source_run_id,
            )
        )
        return StoreOutcome.STORED
