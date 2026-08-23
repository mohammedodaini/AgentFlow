# ADR-0007: Document bytes live in object storage, behind a protocol

- **Status:** accepted
- **Date:** 2026-08-12
- **Milestone:** M5

## Context

M5 accepts uploaded files. Two questions had to be answered before a single
byte could be written: where do the bytes go, and what does the rest of the
application know about that?

The tempting answer to the first is "Postgres, in a `bytea` column": one
datastore, one backup, transactional with the metadata. It is wrong at a scale
this project will reach almost immediately. Every `SELECT *` drags the blob
across the wire; every backup and every replica carries it; and the single most
frequent query this table will ever serve is "is this document ready yet?",
which should not have to step over a 40 MB PDF to answer.

The second question is the one with a real trade in it. Nothing runs MinIO or a
GCS emulator on a laptop for a learning project, and nothing serves production
traffic off a container's filesystem. So local development and production are
*guaranteed* to differ here, which is not true of Postgres or Redis, where the
same software runs in both.

## Decision

`documents` stores metadata and an opaque `storage_uri`. The bytes live behind
`app/storage/`, a new top-level package containing an `ObjectStorage` protocol
and one implementation, `LocalObjectStorage`.

**A `Protocol`, not an abstract base class.** A backend does not have to import
from us to satisfy it, and, more usefully, a test double is a plain class
with three methods rather than a subclass carrying inherited machinery.

**Four members, and no more.** `put`, `get`, `delete`, and a key builder. No
listing, no signed URLs, no copy, no multipart. Each of those is easy to add
against a real requirement and impossible to remove once something depends on
it, and the ones a filesystem cannot emulate honestly, signed URLs above all
would make the local backend a lie.

**All three methods are `async`,** even though the local backend's I/O is
synchronous. Otherwise swapping in a network-backed implementation would change
every call site from `x` to `await x`, which is exactly the leak the interface
exists to prevent. The local backend runs its filesystem calls through
`asyncio.to_thread`, because a blocked event loop stops serving *every* request.

**A new top-level package,** rather than a module inside an existing one.
`core/` must stay a dependency-free leaf; `rag/` is about parsing, not
persistence; `db/` is about Postgres. Storage is its own concern with its own
lifecycle, built once per process by `create_storage(settings)`: the same
builder shape `create_engine` and `create_redis_client` already use, and
necessary because there are two processes that each need one: the API and the
arq worker.

**Keys are tenant-first:**

```
organizations/<org-uuid>/documents/<doc-uuid>/<sanitized-filename>
```

The tenant leading the path makes "erase everything belonging to this customer"
a prefix operation, which is what a GDPR erasure request actually asks for
and makes a per-tenant bucket policy or IAM condition expressible later. A flat
`documents/<uuid>` layout can do neither.

## Consequences

**Good.** The metadata table stays small and fast. Swapping in GCS or S3 at M16
is one new class and one line in `create_storage`; nothing else in the codebase
mentions a filesystem. Local development needs no extra service, so `make dev`
still works on a fresh clone with only Postgres and Redis running.

**Bad.** There is now an indirection with one implementation behind it, which
is over-engineering in most situations. The justification here is that the
second implementation is scheduled rather than hypothetical, and the boundary
costs three methods.

**Also bad.** The local backend is not a faithful simulation of a bucket. It
has no eventual consistency, no per-object ACLs and no network failures, so
code that would break against real object storage can pass every test here. The
mitigation is that the interface is too small to hide much, but "it works
locally" is weaker evidence for this component than for any other in the system.

**Worth knowing.** `storage_uri` is deliberately absent from `DocumentRead`.
Publishing it would freeze a private key layout into the public API and hand
out precisely what someone would need if a bucket were ever misconfigured.

## Alternatives rejected

**Bytes in Postgres (`bytea` or large objects).** Transactional with the
metadata, which is a genuine advantage: no orphaned objects, no compensating
deletes. Rejected on the read cost above, and because it puts customer file
storage on the most expensive and hardest-to-scale tier in the system.

**Writing to the filesystem directly, with no interface.** The simplest
possible thing, and it would work today. Rejected because the replacement is
scheduled: the day it arrives, every call site changes from sync to async,
which is the one refactor that cannot be done mechanically.

**Reaching for a library like `fsspec` or `smart_open` now.** Both solve this
properly and both are more machinery than three methods justify. Worth
revisiting at M16 if the GCS implementation turns out to be more than fifty
lines.

**Storing the extracted text as a second object.** Considered, because
extraction is the slow and fragile step and M6 will want the text again.
Rejected as speculative: it invents a derived artifact with its own lifecycle
(invalidation, cleanup on re-ingest) before anything reads it. Re-extracting at
M6 is cheap next to embedding, and the decision can be revisited with real
numbers.
