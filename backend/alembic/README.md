# Migrations

Alembic, async, wired to `Settings` and `app.models.Base`. Run everything from
the repo root via `make`.

```bash
make makemigration m="add documents table"   # autogenerate from the models
make migrate                                 # apply up to head
```

## The workflow

1. **Change the model** in `app/models/`, and add it to `app/models/__init__.py`
   if it is a new table. Autogenerate only sees what that package imports.
2. **Generate** with `make makemigration m="..."`. The message becomes part of
   the filename, so write it like a commit subject.
3. **Read the generated file.** Always. Autogenerate is a first draft, not an
   answer, see the limits below.
4. **Apply** with `make migrate`, then confirm the models and the database
   agree:

   ```bash
   uv run alembic check      # "No new upgrade operations detected."
   ```

5. **Rehearse the rollback** before it is ever needed:

   ```bash
   uv run alembic downgrade -1 && uv run alembic upgrade head
   ```

## What autogenerate does not catch

It diffs `Base.metadata` against the live schema, so it sees tables, columns,
indexes and constraints, and nothing else:

- **Postgres enum types.** `CREATE TABLE` creates the type; `DROP TABLE` leaves
  it behind. The initial migration drops `membership_role` by hand for exactly
  this reason. Adding a value later needs `ALTER TYPE ... ADD VALUE`, written
  by you.
- **Data migrations.** Backfilling a new NOT NULL column is three steps: add it
  nullable, backfill, then set NOT NULL. Autogenerate writes only step one.
- **Anything renamed.** A renamed column reads as "drop one, add another"
  a rename that destroys the data. Rewrite it as `op.alter_column`.
- **Table locks.** `ALTER TABLE` takes an ACCESS EXCLUSIVE lock. On a large,
  busy table that is downtime, however small the diff looks.

## Useful commands

```bash
uv run alembic current            # which revision this database is on
uv run alembic history --verbose  # the full chain
uv run alembic upgrade head --sql # print SQL instead of applying it
```

The last one is how migrations reach a database CI may not touch: generate the
SQL, have it reviewed, apply it during a change window.

## Conventions

- **One migration per pull request**, matching one schema change.
- **Never edit a migration that has been applied anywhere but your laptop.**
  Write a new one. Editing history means two databases silently disagree about
  what revision `abc123` contains.
- Constraint names come from `NAMING_CONVENTION` in `app/models/base.py`.
  Never name one by hand: the convention is what keeps diffs reviewable.
