# ADR-0001: Record architecture decisions

**Status:** accepted · **Date:** 2026-07-10

## Context
Significant technical decisions (pgvector vs. dedicated vector DB, arq vs.
Celery, monolith vs. microservices) are easy to make and easy to forget the
reasons for. Six months later, "why did we do it this way?" has no answer,
and decisions get relitigated or accidentally reversed.

## Decision
We record every significant architectural decision as a short numbered ADR
in `docs/adr/`, using this template: **Context** (the forces at play),
**Decision** (what we chose), **Consequences** (what becomes easier/harder).
ADRs are immutable; a change of course is a *new* ADR that supersedes the old.

## Consequences
- New contributors can read the decision history instead of reverse-
  engineering intent from code.
- Slight writing overhead per big decision (~15 minutes) — worth it.
