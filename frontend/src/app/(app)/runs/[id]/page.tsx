import Link from "next/link";
import { notFound } from "next/navigation";

import { Badge, Card } from "@/components/ui";
import { ApiError, apiFetch } from "@/lib/api";
import type { AgentRun } from "@/lib/types";

export const metadata = { title: "Run · AgentFlow" };

/**
 * The trace, shown to the user rather than kept for operators.
 *
 * ADR-0012's argument, rendered: when an answer looks wrong, "which passages did
 * it find, and did it search twice?" is often a question the person who asked can
 * answer faster than we can. An interface that shows its working earns trust a
 * bare answer does not.
 *
 * `checkpoint` is absent from the API response by design and so cannot appear
 * here — the graph's internals are not a public contract.
 */
export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let run: AgentRun;

  try {
    run = await apiFetch<AgentRun>(`/agent-runs/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      notFound();
    }

    throw error;
  }

  return (
    <div className="space-y-6">
      <Link href="/chat" className="text-sm text-[--color-muted] hover:text-[--color-ink]">
        ← Back
      </Link>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">{run.agent_name} run</h1>
        <Badge tone={run.status === "succeeded" ? "ok" : run.status === "failed" ? "bad" : "warn"}>
          {run.status}
        </Badge>
        <span className="text-sm text-[--color-muted]">
          {run.total_tokens.toLocaleString()} tokens
          {run.duration_ms === null ? null : ` · ${run.duration_ms} ms`}
          {/* Shown even when zero, because 0.000000 means "nobody has told this
              system what it pays" rather than "this run was free" (M12). */}
          {` · $${run.cost_usd}`}
        </span>
      </div>

      {run.error ? (
        <Card className="border-[--color-danger] p-4 text-sm text-[--color-danger]">
          {run.error}
        </Card>
      ) : null}

      <Card>
        <ol className="divide-y divide-[--color-border]">
          {run.steps.map((step) => (
            <li key={step.step_index} className="space-y-2 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge>{step.node_name}</Badge>
                {step.tool_name ? (
                  <span className="text-xs text-[--color-muted]">{step.tool_name}</span>
                ) : null}
                <span className="ml-auto text-xs text-[--color-muted]">{step.latency_ms} ms</span>
              </div>

              {step.tool_output ? (
                <pre className="overflow-x-auto rounded-lg bg-[--color-canvas] p-3 text-xs text-[--color-muted]">
                  {JSON.stringify(step.tool_output, null, 2)}
                </pre>
              ) : null}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}
