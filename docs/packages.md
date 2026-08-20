# Package Choices

Every dependency is a liability (security surface, upgrade burden, learning
cost) — each one below has to justify itself. ✅ = required, 🔶 = optional /
added at its milestone. "Popularity" = rough industry adoption for this role.

## Web framework
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| fastapi | Async API framework; Pydantic-native validation; free OpenAPI docs | ✅ | Django (batteries, sync-first), Flask (minimal, no types), Litestar (similar, smaller community) | The default for new Python APIs |
| uvicorn | ASGI server that actually runs the app | ✅ | hypercorn, granian | Standard pairing with FastAPI |
| python-multipart | Form/file upload parsing (PDF uploads) | ✅ | none practical | Standard |

## Validation & settings
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| pydantic v2 | Typed validation for API schemas and agent state; Rust-core fast | ✅ | msgspec (faster, fewer features), attrs+cattrs | Dominant |
| pydantic-settings | Typed config from env vars — the 12-factor pattern | ✅ | python-dotenv alone (untyped) | Standard with FastAPI |

## Database
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| sqlalchemy 2 (async) | ORM + query builder; the 2.x API is fully typed | ✅ | Django ORM (tied to Django), SQLModel (thin SQLAlchemy wrapper, lags releases), raw asyncpg (no ORM) | Dominant |
| asyncpg | Fastest async Postgres driver | ✅ | psycopg 3 (also good, sync+async) | Co-standard |
| alembic | Schema migrations as versioned scripts | ✅ | none serious for SQLAlchemy | Standard |
| pgvector (python) | Vector column type for embeddings in Postgres | ✅ | qdrant-client / weaviate-client / pinecone (dedicated stores) | Fast-growing default for <10M vectors |

## Auth & security
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| pyjwt | Encode/verify JWTs | ✅ | python-jose (maintenance concerns), authlib.jose | Standard |
| argon2-cffi | Password hashing — Argon2id is the current OWASP recommendation | ✅ | bcrypt (fine, older) | Recommended modern choice |
| authlib | OAuth 2.0 client flows for Google/Slack/Notion/GitHub | ✅ | requests-oauthlib (sync), hand-rolled (don't) | Leading OAuth lib |
| cryptography | Encrypt OAuth tokens at rest (Fernet). **Declared explicitly at M11** rather than inherited through authlib — a transitive dependency is one an upstream upgrade can remove, and discovering that at import time in production is a deploy failing for a reason nobody changed | ✅ | none serious | Standard |

## Frontend (M13)
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| next | App Router, Server Components and Server Actions. The Server Action is not a convenience here — it is what keeps the password out of the client bundle and makes `revalidatePath` available, which is what keeps the UI honest after a write (ADR-0016) | ✅ | Remix, SvelteKit, a Vite SPA (would force tokens into the browser) | Dominant |
| react / react-dom | 19, for `useActionState` — the pending state that stops a slow agent turn being submitted twice | ✅ | — | Standard |
| tailwindcss | 4. Design tokens in one `@theme` block; no component library vendored (see frontend/README.md) | ✅ | CSS modules, vanilla-extract | Standard |
| typescript | `strict`, matching the backend's `mypy --strict` | ✅ | none serious | Standard |
| server-only | One import that turns "this must never reach the browser" into a *build error*. Three lines of package for the guarantee that `api.ts` cannot be imported by a Client Component | ✅ | a lint rule (advisory, not enforced) | Standard |
| playwright | The smoke test. `pnpm build` proves it compiles; only a browser proves somebody can use it — and three of its assertions (httpOnly, `document.cookie`, no `checkpoint` leak) would regress silently | dev | Cypress (heavier), Puppeteer (no cross-browser) | Leading |

## Redis & background work
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| redis-py | Cache, rate limiting, queue broker | ✅ | none serious | Standard |
| arq | Async task queue on Redis, by the pydantic author | ✅ | Celery (heavyweight, sync-first, most popular), Dramatiq, TaskIQ | Niche but ideal for async stacks |

## LLM & agents
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| anthropic | Primary LLM SDK | ✅ | openai SDK (also OpenAI-compatible gateways) | Top-2 |
| langgraph | Graph orchestration + checkpointing + interrupts (human approval) | ✅ | Build-your-own loop (fine for 1 agent, painful for HITL), CrewAI/AutoGen (higher-level, less control) | Leading agent-orchestration lib |
| langchain-core | Minimal interfaces LangGraph builds on (we do NOT take the full langchain kitchen sink) | ✅ | — | — |
| openai | Embeddings and model comparisons | 🔶 | voyageai, cohere, local sentence-transformers | Top-2 |

## RAG / documents
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| pypdf | PDF text extraction | ✅ | pymupdf (faster, AGPL), unstructured (heavy, handles everything) | Common default |
| tiktoken | Token counting for chunking budgets | ✅ | anthropic's counter API | Standard |

## Integrations
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| httpx | Async HTTP client — one client for all integrations | ✅ | aiohttp, requests (sync) | Standard modern choice |
| google-api-python-client + google-auth | Gmail/Calendar/Drive official SDKs | 🔶 | raw REST via httpx | Official |
| slack-sdk, PyGithub, stripe, notion-client | Official/canonical SDKs per product | 🔶 | raw REST | Official |

## Observability
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| structlog | Structured (JSON) logging with bound context (request_id, org_id) | ✅ | loguru (pretty, less structured), stdlib logging (verbose) | Production standard |
| sentry-sdk | Error tracking with traces | 🔶 | rollbar, honeybadger | Dominant |
| opentelemetry-* | Distributed tracing incl. LLM spans | 🔶 | langsmith/langfuse (LLM-specific) | Industry standard |

## Testing & quality
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| pytest (+asyncio, +cov) | Test runner | ✅ | unittest (verbose) | Universal |
| faker | Realistic test data | ✅ | hand-rolled fixtures | Standard |
| ruff | Linter + formatter, replaces black+flake8+isort, 100x faster | ✅ | black+flake8+isort trio | New standard |
| mypy | Static type checking (strict) | ✅ | pyright (faster, used by IDEs) | Co-standard |
| pre-commit | Run checks at commit time | ✅ | husky-style git hooks | Standard |

## Tooling
| Package | Why | Req | Alternatives | Popularity |
|---|---|---|---|---|
| uv | Package/venv manager — resolver 10-100x faster than pip; lockfile | ✅ | poetry, pip-tools, pip | Rapidly becoming the standard |
