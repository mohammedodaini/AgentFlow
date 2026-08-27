# AI / Agent Architecture

Built on **LangGraph** (graph-structured orchestration with checkpointing
which we need for human-in-the-loop pauses). Design only; no implementation
until its milestone.

## Topology: supervisor pattern

```
                        ┌────────────────┐
      user message ───► │   Supervisor   │ ◄── long-term memory (recall)
                        └───────┬────────┘
                 routes to one or a plan of:
      ┌─────────┬─────────┬────┴────┬──────────┬──────────┐
      ▼         ▼         ▼         ▼          ▼          ▼
  ┌───────┐ ┌───────┐ ┌────────┐ ┌───────┐ ┌────────┐ ┌────────┐
  │Planner│ │  RAG  │ │Research│ │ Email │ │Calendar│ │Proposal│
  └───┬───┘ └───┬───┘ └───┬────┘ └───┬───┘ └───┬────┘ └───┬────┘
      │         │         │          │         │          │
      └─────────┴────► tools = services & integrations ◄──┘
                        (never raw DB / raw APIs)
                                 │
                    side-effect tools require an
                    APPROVAL interrupt before executing
                                 │
                        ┌────────▼───────┐     ┌────────────┐
                        │  Memory Agent  │     │ Evaluation │ (offline)
                        │ (extract/store)│     │   Agent    │
                        └────────────────┘     └────────────┘
```

## The agents

| Agent | Job | Typical tools |
|---|---|---|
| **Supervisor** | Classify intent, route to a specialist or ask Planner for a multi-step plan, own the conversation | route(), recall_memory() |
| **Planner** | Decompose complex requests ("research this lead and draft an intro email") into ordered steps for other agents | none: pure reasoning |
| **RAG** | Answer questions over the org's knowledge base with citations | search_chunks(), fetch_document() |
| **Research** | Gather external info on leads/companies | web_search(), fetch_url() |
| **Email** | Draft, summarize, and (after approval) send email | search_email(), draft_email(), send_email()* |
| **Calendar** | Find availability, schedule meetings, create reminders | list_events(), create_event()*, create_reminder()* |
| **Proposal** | Generate proposals from templates + RAG context + research | search_chunks(), render_template() |
| **Memory** | After each run: extract durable facts, store/update/decay memories | store_memory(), search_memory() |
| **Evaluation** | Offline: score runs against golden datasets and rubrics (LLM-as-judge) | read traces, score() |

`*` = side-effect tool → always gated by human approval.

## How they communicate

1. **Shared graph state, not chat messages.** Within a run, agents are nodes
   in one LangGraph graph passing a typed state object (conversation context,
   plan, intermediate results). No agent-to-agent free-text protocols: those
   are where multi-agent systems go to die.
2. **Supervisor is the only entry point.** Specialists never call each other
   directly; the supervisor (guided by the Planner's plan) sequences them.
   This keeps the call graph a tree, which is debuggable.
3. **Tools are the only way to touch the world.** Every tool wraps a service
   method, so tenancy scoping, logging, and permission checks apply to
   agents automatically, identically to human API users.
4. **Checkpointing to Postgres.** Every state transition persists. This gives
   us (a) resume after crash, (b) human-approval pauses of arbitrary length,
   (c) full traces in `agent_runs` / `agent_steps`.
5. **Memory is asynchronous.** The Memory agent runs after the response is
   sent, memory extraction should never add latency to the user.

## Design principles

- **Start with ONE agent.** The roadmap builds a single RAG agent first;
  the supervisor and specialists arrive only when a single agent measurably
  fails at the breadth of tasks. Premature multi-agent architecture is the
  most common failure mode in this space.

  **This precondition was met at M15, and the measurement is committed.**
  `app/evaluation/data/routing.json` holds twenty hand-written instructions;
  the single-agent world scores **0.300** on them and the supervisor **1.000**,
  and both numbers are re-measured by every `make eval`. The rule above is
  therefore still enforced rather than merely honoured: the next agent needs
  its own evidence, not this one's.

  What M15 built is the supervisor and the planner. **Research, Proposal,
  Memory and Evaluation remain unbuilt on purpose**: the table above is a
  design, not a checklist. Evaluation lives in a runner (M8) and memory
  extraction in a worker task (M10), both deliberately, because neither has a
  branch to be a graph about; Research needs a web search tool this
  environment cannot have; Proposal is a template renderer nobody has asked
  for.
- **Deterministic where possible.** If code can do it (parse a date, format
  a template), code does it. LLM calls are for judgment, not plumbing.
- **Every run is billable and traceable.** Tokens and cost recorded per run;
  every tool call recorded per step.
- **Evals before cleverness.** No prompt change ships without the evaluation
  harness confirming no regression on the golden set.
