# Learning Roadmap

Rules of engagement (per our mentoring contract):
1. Each milestone: **concept → build together → quiz → exercise → code review → refactor → next.**
2. No skipping ahead. Later milestones assume mastery of earlier ones.
3. Every milestone ends with `make check` green and something demoable.

## Phase 1 — Foundations
- **M1. Skeleton that runs**: config (pydantic-settings), structlog, `/health`
  endpoint, docker-compose up, CI green. *Learn:* app factory, DI, 12-factor.
- **M2. Database layer**: async SQLAlchemy, session-per-request, Alembic,
  `users`/`organizations`/`memberships` tables. *Learn:* migrations, async ORM.
- **M3. Authentication**: register/login, Argon2, JWT access+refresh,
  `get_current_user`, org scoping. *Learn:* token lifecycles, OWASP basics.
- **M4. Testing discipline**: pytest fixtures, test DB, factories, coverage
  gate. *Learn:* the testing pyramid (this discipline carries the whole project).

## Phase 2 — Knowledge
- **M5. Document upload**: file storage, `documents` table, background
  ingestion via arq. *Learn:* task queues, 202-pattern.
- **M6. RAG pipeline**: chunking, embeddings, pgvector HNSW, retrieval
  endpoint with citations. *Learn:* embeddings, chunking tradeoffs.
- **M7. First LLM feature**: `/ask` — retrieval + Claude answer with sources.
  *Learn:* prompt design, token budgets, streaming.
- **M8. RAG evaluation**: golden Q&A set, retrieval metrics (recall@k),
  LLM-as-judge answer scoring. *Learn:* evals as tests.

## Phase 3 — Agency
- **M9. First agent (LangGraph)**: single RAG agent with tools, `agent_runs`
  + `agent_steps` tracing. *Learn:* graphs, state, checkpointing.
- **M10. Conversations & memory**: `conversations`/`messages`, memory
  extraction + recall. *Learn:* context management.
- **M11. First OAuth integration (Google)**: connect flow, encrypted token
  storage, Calendar read. *Learn:* OAuth 2.0 end to end.
- **M12. Human-in-the-loop**: approval records, LangGraph interrupts,
  Calendar write + Email draft/send behind approval. *Learn:* safe agency.

## Phase 4 — Product
- **M13. Frontend**: Next.js scaffold, auth pages, chat UI, approval inbox.
- **M14. More integrations**: Slack, Notion, GitHub, Stripe (one pattern,
  repeated — by now integrations are routine).
- **M15. Multi-agent**: supervisor + planner + specialists, only where the
  single agent measurably falls short.
- **M16. Production**: Dockerized deploy, monitoring, Sentry, rate limiting,
  security hardening pass, load test.

Each milestone is roughly a focused week. Order is deliberate: **you cannot
debug an agent (M9) if you can't trust your tests (M4), and you cannot trust
agent answers (M7) without retrieval you've measured (M8).**
