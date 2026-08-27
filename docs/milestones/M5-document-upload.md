# M5: Document upload and background ingestion

**Status:** complete (2026-08-12) · **Gate:** `make check` green · **Tests:** 236 passing (was 156) · **Coverage:** 98.9%

The first milestone where the application does something a user would describe
as work, and the first where two processes have to agree about a fact. Almost
everything interesting in it comes from that second property.

## What was built

| Piece | Where |
|---|---|
| `documents` + `tasks` tables, three native enums | [`app/models/document.py`](../../backend/app/models/document.py), [`task.py`](../../backend/app/models/task.py) |
| Object storage behind a protocol | [`app/storage/`](../../backend/app/storage/) |
| PDF / text extraction | [`app/rag/ingestion.py`](../../backend/app/rag/ingestion.py) |
| Tenant-scoped queries | [`app/repositories/document_repository.py`](../../backend/app/repositories/document_repository.py) |
| Upload orchestration, size and type limits | [`app/services/document_service.py`](../../backend/app/services/document_service.py) |
| arq producer (`JobQueue` protocol) | [`app/workers/queue.py`](../../backend/app/workers/queue.py) |
| arq worker entrypoint + ingestion task | [`app/workers/settings.py`](../../backend/app/workers/settings.py), [`tasks/ingestion.py`](../../backend/app/workers/tasks/ingestion.py) |
| Four endpoints, 202 + polling | [`app/api/v1/routes/documents.py`](../../backend/app/api/v1/routes/documents.py) |

## The 202 pattern, concretely

```
POST /documents            → validate type · read with a size cap · store bytes
                             · insert document(pending) + task(queued) · COMMIT
                             · enqueue · 202
worker                     → task(running) · document(processing)
                             · fetch bytes · extract text (in a thread)
                             · document(ready|failed) · task(succeeded|failed)
GET  /documents/{id}       → whatever the worker last wrote
```

Three details carry most of the weight.

**202, not 201.** 201 claims the resource is finished and available. Nothing
has been parsed yet, and from M6 nothing will be searchable yet either. 202
says "accepted, not complete", which is exactly what happened, and it tells the
client to poll.

**The size limit is enforced while reading, not after.** Checking
`Content-Length` trusts the client, and measuring the file once it is in memory
has already spent what the limit was meant to save. The upload is read in 1 MB
chunks and refused the moment it passes the ceiling.

**Bytes are written before the row that points at them,** and if the database
work then fails, the object is deleted again. An orphaned file costs money and
confuses an operator; a row pointing at nothing breaks reads. The risk is taken
in the recoverable direction.

## The decision that took three attempts

**Nothing may be enqueued before the data it refers to is committed.** A worker
is milliseconds away and lives in another process, so a job pushed inside the
transaction can be dequeued before the row is visible, leaving a document
`pending` forever with no error anywhere.

The first implementation used `BackgroundTasks`, on the documented belief that
dependency teardown runs before background tasks. On FastAPI 0.141 it is the
other way round. The e2e test recorded `['enqueue', 'commit']` on its first run,
the race, reproduced immediately, before any of this reached a queue with
real load on it.

The route now commits explicitly and then enqueues. Full reasoning, the
alternatives, and the crash window that remains, in
[ADR-0008](../adr/0008-work-is-enqueued-only-after-the-transaction-commits.md).

Storage got its own decision:
[ADR-0007](../adr/0007-object-storage-behind-a-protocol.md).

## Bugs this milestone found

Five, none of them by reading the code.

**`utf-8` was tried before `utf-8-sig`.** A file with a byte order mark decodes
*successfully* as UTF-8, keeping the mark as a U+FEFF character, so the sig
codec was never reached. Every such document would have begun with an invisible
stray character, and so would its first citation at M6.

**cp1252 decodes almost anything.** Latin-1 was excluded from the fallback list
for exactly that reason, and the replacement has the same flaw: only five of
cp1252's 256 bytes are undefined. A `.zip` renamed `.txt` would have become a
`ready`, searchable document made entirely of noise, worse than a failure
because nothing about it looks wrong. Fixed with a printable-character check.

**`mkdir` sat outside the `try`** in the atomic write, so a directory-creation
failure escaped as a raw `OSError` instead of a `StorageError`: the one
translation that module exists to guarantee. Then the fix exposed a second
layer: the `except` block's own `unlink` raised on the same bad path, replacing
the real error with a confusing one.

**The Postgres enums outlived their tables again.** Autogenerate wrote a
`downgrade()` that drops `documents` and `tasks` and leaves `document_status`,
`document_source` and `task_status` behind, so the next `upgrade` fails on a
type that already exists. Same defect as M2, caught the same way, by actually
running `alembic downgrade -1 && alembic upgrade head` rather than reading it.

**UUIDv7 ordering within one millisecond is arbitrary.** A docstring claimed id
order *is* chronological order. Our `uuid7()` is the pure-random variant of
RFC 9562 §5.7 with no monotonic counter, so ids minted in the same millisecond
sort by their random bits. The property pagination actually needs: a *total
stable* order, still holds, and is the one now documented.

## Verified at runtime

Not only in tests. A real uvicorn, a real arq worker, real Redis and Postgres,
driven with curl:

```
POST /documents (real PDF)      → HTTP 202, status "pending"
poll 1s later                   → status "ready"
tasks row                       → succeeded, attempts=1, result={"characters": 61}
on disk                         → organizations/<org>/documents/<doc>/handbook.pdf
truncated PDF                   → status "failed",
                                  "Could not read the PDF: the file may be
                                   corrupt or truncated (Stream has ended
                                   unexpectedly)."
.exe upload                     → HTTP 415, listing the types that do work
no Authorization                → HTTP 401
no X-Organization-Id            → HTTP 422
DELETE                          → HTTP 204, row gone, 0 files left
```

## Gate

```
$ make check
All checks passed!                                   # ruff
Success: no issues found in 158 source files         # mypy strict
Required test coverage of 97.0% reached.
Total coverage: 98.86%
236 passed in 15.79s

$ uv run alembic check
No new upgrade operations detected.

$ make test-pyramid
unit         128/236
integration   41/236
e2e           67/236
```

## Known gaps, deliberately left

- **No sweeper for stuck tasks.** A `tasks` row that stays `queued`, because
  the process died between commit and enqueue, or Redis was down, is invisible
  today. The row exists precisely so this is fixable; the publisher does not.
  ADR-0008, M16.
- **`Content-Type` is trusted.** A renamed executable announced as
  `application/pdf` is stored. Bounded rather than dangerous, nothing executes
  it, extraction fails, the document ends `failed`, but magic-byte sniffing
  belongs in the M16 hardening pass.
- **No virus scanning.** Same milestone, and a real requirement before any
  customer uploads anything.
- **No OCR.** A scanned PDF fails with a message saying so, which is the honest
  behaviour; adding OCR is its own project.
- **No per-tenant quota.** `byte_size` is stored so the question is answerable,
  but nothing asks it yet.
- **Extraction is thrown away at M5.** M6 will re-parse during chunking. Cheap
  next to embedding, and it avoids inventing a derived artifact with its own
  invalidation rules before anything reads it (ADR-0007).
- **The worker is not in `docker-compose.yml`.** `make worker` runs it by hand.

## Reproduce

```bash
make up && make migrate
make check
make dev            # terminal 1
make worker         # terminal 2, without this, uploads stay "pending" forever
```
