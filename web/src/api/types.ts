/**
 * DTO HTTP-контракта backend ↔ web (contract_version 1.0.0).
 * Источник истины — backend/api_models.py и contracts/schemas/*.schema.json.
 */
export type ActionType =
  | "answer"
  | "clarify"
  | "collect_contacts"
  | "lead_ready"
  | "escalate"
  | "out_of_scope";

export interface Health {
  status: "ok";
  contract_version: string;
  engine: string;
  build_sha?: string | null;
}

export interface SessionCredentials {
  session_id: string;
  client_token: string;
}

export interface SessionCreated extends SessionCredentials {
  contract_version: string;
  status: "active";
  created_at: string;
}

export interface MessageView {
  id: number;
  seq: number;
  role: "user" | "assistant";
  content: string;
  request_id: string;
  in_context: boolean;
  created_at: string;
}

export interface ContactInfo {
  full_name: string | null;
  phone: string | null;
  company: string | null;
  region: string | null;
}

export interface LeadSummary {
  task: string;
  known_facts: string[];
  assumptions: string[];
  discussed_solutions: string[];
  unknowns: string[];
  next_step: string;
}

export type LeadStatus = "none" | "draft" | "ready" | "saved";

export interface LeadView {
  lead_id: string | null;
  session_id: string;
  status: LeadStatus;
  contact: ContactInfo;
  contact_refused: boolean;
  callback_requested: boolean;
  missing_fields: string[];
  summary: LeadSummary | null;
  offer_id: string | null;
  revision: number;
  saved_at: string | null;
  delivery: "stored_only";
  status_text: string;
}

export interface SessionView {
  contract_version: string;
  session_id: string;
  status: "active" | "closed";
  created_at: string;
  closed_at: string | null;
  messages: MessageView[];
  lead: LeadView | null;
}

export interface TurnRequest {
  request_id: string;
  text: string;
}

export interface TurnResponse {
  contract_version: string;
  session_id: string;
  request_id: string;
  status: "completed";
  user_message_id: number;
  assistant_message_id: number;
  answer: string;
  action: ActionType;
  clarifying_question: string | null;
  lead: LeadView | null;
  debug_available: boolean;
  replayed: boolean;
}

export interface ContactsUpdate {
  full_name?: string | null;
  phone?: string | null;
  company?: string | null;
  region?: string | null;
  callback_requested?: boolean | null;
}

export interface LeadSaveRequest {
  idempotency_key: string;
  offer_id: string;
}

export interface Evidence {
  evidence_id: string;
  doc_id: string;
  doc_title: string | null;
  section: string | null;
  text: string;
  score: number | null;
  retriever: string | null;
}

export interface StageTiming {
  name: string;
  ms: number;
  status: "ok" | "error" | "skipped";
  error: string | null;
}

export interface TraceView {
  contract_version: string;
  trace_id: string;
  session_id: string;
  request_id: string;
  attempt: number;
  status: "completed" | "failed" | "cancelled";
  engine: string;
  created_at: string;
  timings: StageTiming[];
  retrieved: Evidence[];
  used: Evidence[];
  used_missing_ids: string[];
  action: string | null;
  error: { stage?: string; type?: string; message?: string } | null;
}

export interface ErrorBody {
  code: string;
  message: string;
  request_id: string | null;
  retryable: boolean;
}
