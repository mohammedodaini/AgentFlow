/**
 * The shapes the backend actually returns.
 *
 * Hand-written rather than generated from the OpenAPI schema, and that is a
 * deliberate trade worth naming. Generation would keep these in perfect sync and
 * add a build step, a generator dependency and a large file nobody reads. These
 * are hand-written because they are *small* — the backend's response schemas are
 * whitelists (`app/schemas/common.py`), so there is little to mirror.
 *
 * The cost is real: a backend field rename breaks the UI at runtime rather than
 * at build time. If that bites twice, generate them — but generating them now
 * would be paying the complexity before the pain.
 */

export type UUID = string;

/** `app/schemas/common.py::Page` — every list endpoint answers this shape. */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Organization {
  id: UUID;
  name: string;
  slug: string;
}

export interface Membership {
  organization: Organization;
  role: "owner" | "admin" | "member";
}

export interface User {
  id: UUID;
  email: string;
  full_name: string | null;
}

export interface Conversation {
  id: UUID;
  title: string;
  archived_at: string | null;
  created_at: string;
}

export interface Message {
  id: UUID;
  role: "user" | "assistant" | "tool";
  content: string;
  agent_run_id: UUID | null;
  token_usage: Record<string, number> | null;
  created_at: string;
}

/** What `POST /conversations/{id}/messages` answers with — both turns. */
export interface Turn {
  conversation: Conversation;
  user_message: Message;
  assistant_message: Message;
}

export interface DocumentRead {
  id: UUID;
  title: string;
  status: "pending" | "processing" | "ready" | "failed";
  mime_type: string;
  byte_size: number;
  error: string | null;
  created_at: string;
}

export interface Approval {
  id: UUID;
  agent_run_id: UUID;
  status: "pending" | "approved" | "rejected" | "expired";
  summary: string;
  requested_action: Record<string, unknown>;
  decided_by: UUID | null;
  decided_at: string | null;
  expires_at: string;
  reason: string | null;
  created_at: string;
}

export interface Proposal {
  agent_run_id: UUID;
  status: string;
  approval: Approval | null;
  message: string | null;
}

export interface Integration {
  id: UUID;
  provider: string;
  status: "active" | "revoked" | "disconnected";
  external_account_id: string | null;
  scopes: string[];
  created_at: string;
}

export interface Supervised {
  run: AgentRun;
  /** Null exactly when the supervisor refused — a success with nothing downstream. */
  delegated: AgentRun | null;
  /** The row a human must decide on, present when the specialist proposed a side effect. */
  approval: Approval | null;
  reason: string;
}

export interface ProviderRead {
  provider: string;
  /** What connecting will request permission for. Empty for Notion, which grants
   *  access per page rather than per scope. */
  scopes: string[];
}

export interface ConnectStart {
  authorize_url: string;
  provider: string;
}

export interface AgentStep {
  step_index: number;
  node_name: string;
  tool_name: string | null;
  tool_input: Record<string, unknown> | null;
  tool_output: Record<string, unknown> | null;
  latency_ms: number;
  tokens: number;
}

export interface AgentRun {
  id: UUID;
  agent_name: string;
  status: string;
  error: string | null;
  total_tokens: number;
  cost_usd: string;
  duration_ms: number | null;
  created_at: string;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  steps: AgentStep[];
}

/** `app/api/errors.py` — every failure answers this envelope. */
export interface ApiErrorBody {
  error: { code: string; message: string };
}
