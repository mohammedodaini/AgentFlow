# Database Design

PostgreSQL 17 + pgvector. Conventions: UUID (v7) primary keys, `created_at`
/ `updated_at` on every table, snake_case, plural table names, soft deletes
only where audit requires it.

**The multi-tenancy rule that shapes everything:** AgentFlow is B2B SaaS, so
almost every table hangs off `organizations`, not `users`. A user belongs to
an org through `memberships`; every query is scoped by `organization_id`.
Getting this wrong at the start is the single most expensive schema mistake
in SaaS, retrofitting tenancy means touching every table and every query.

## Entity map

```
organizations ──< memberships >── users
     │
     ├──< integrations ──< oauth_tokens
     ├──< documents ──< document_chunks (embedding vector)
     ├──< conversations ──< messages
     ├──< agent_runs ──< agent_steps
     │        └──< approvals
     ├──< memories
     ├──< tasks
     └──< events (audit log)
```

## Tables

### Identity & tenancy
- **users**: `id, email (unique), password_hash, full_name, is_active, is_verified`.
  Auth identity only; everything business-related keys on organization.
- **organizations**: `id, name, slug (unique), plan`. The tenant.
- **memberships**: `user_id FK, organization_id FK, role (owner|admin|member)`
  unique on `(user_id, organization_id)`. Many-to-many with a role payload
  this is why it's a real table, not a join shortcut: roles, invites, and
  seat-billing all live here later.

### Integrations
- **integrations**: `organization_id FK, provider (gmail|slack|…), status
  connected_by FK users, scopes, external_account_id`. One row per connected
  product per org.
- **oauth_tokens**: `integration_id FK, access_token (encrypted), refresh_token
  (encrypted), expires_at`. Separate table so token secrets can carry stricter
  access controls and rotation without touching integration metadata.
  **Tokens are encrypted at the application layer before insert.**

### Knowledge base (RAG)
- **documents**: `organization_id FK, uploaded_by FK users, title, source
  (upload|gmail|drive|notion), mime_type, storage_uri, status
  (pending|processing|ready|failed), error`. Metadata only; bytes live in
  object storage, never in Postgres.
- **document_chunks**: `document_id FK, chunk_index, content, token_count
  embedding vector(1536), metadata jsonb`. The retrieval unit. HNSW index on
  `embedding`; unique on `(document_id, chunk_index)`.
  *Why chunks are their own table:* retrieval returns chunks, joins give you
  the parent document for citations, and re-chunking = delete + reinsert
  without touching documents.

### Conversation
- **conversations**: `organization_id FK, user_id FK, title, archived_at`.
- **messages**: `conversation_id FK, role (user|assistant|tool), content
  agent_run_id FK nullable, token_usage jsonb`. Append-only; editing history
  destroys auditability.

### Agent execution
- **agent_runs**: `organization_id FK, conversation_id FK nullable
  triggered_by FK users, agent_name, status (running|paused_for_approval|
  succeeded|failed|cancelled), input, output, error, checkpoint jsonb,
  started_at, finished_at, total_tokens, cost_usd`. One row per top-level
  agent invocation: the unit of observability *and* billing.
- **agent_steps**: `agent_run_id FK, step_index, node_name, tool_name
  tool_input jsonb, tool_output jsonb, latency_ms, tokens`. The trace: every
  LLM call and tool call. This is how you debug "why did the agent do that?"
- **approvals**: `agent_run_id FK, organization_id FK, requested_action jsonb
  (e.g. the full email draft), status (pending|approved|rejected|expired),
  decided_by FK users, decided_at`. Human-in-the-loop is a *database record*,
  not an in-memory flag: it must survive restarts and appear in audits.

### Memory & operations
- **memories**: `organization_id FK, scope (org|user), user_id FK nullable
  content, embedding vector(1536), importance, last_accessed_at, source_run_id
  FK agent_runs`. Long-term agent memory, vector-searchable, decayable.
- **tasks**: `organization_id FK, kind, payload jsonb, status (queued|running|
  succeeded|failed), attempts, result jsonb`. DB mirror of queue jobs so users
  can see progress and ops can retry; Redis holds the queue, Postgres holds
  the truth.
- **events**: `organization_id FK, actor_user_id FK nullable, actor_agent_run_id
  FK nullable, event_type, payload jsonb`. Append-only audit log: every
  login, connect, approval, send. Compliance and debugging both read this.

## Relationship notes

- `users ↔ organizations` is many-to-many **through memberships** because one
  person consults for two companies, and one company has many staff.
- `documents 1, N document_chunks`: cascade delete, chunks are meaningless
  without their document.
- `agent_runs 1, N agent_steps / approvals`: the run is the aggregate root;
  steps and approvals never exist without a run.
- `messages N, 1 agent_runs` (nullable): an assistant message can cite the run
  that produced it, linking chat UX to the execution trace.

## Future scalability
- Partition `events` and `agent_steps` by month when they grow hot.
- `document_chunks.embedding` moves to a dedicated vector store if we pass
  ~10M chunks; the `rag/` layer is the only code that would change.
