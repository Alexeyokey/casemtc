import type { LeadView, MessageView, SessionCredentials } from "../api/types";

export type Phase = "booting" | "ready" | "fatal";
export type LeadOp = "contacts" | "save" | "refuse" | null;
export type FieldName = "phone" | "full_name" | "company";

export interface PendingTurn {
  request_id: string;
  text: string;
}

export interface Notice {
  tone: "error" | "info";
  text: string;
  /** Есть что повторить (pending) — показываем кнопку «Повторить». */
  canRetry?: boolean;
}

export interface ChatState {
  phase: Phase;
  /** Увеличивается при каждой смене сессии: ответы старой сессии отбрасываются. */
  epoch: number;
  session: SessionCredentials | null;
  messages: MessageView[];
  pending: PendingTurn | null;
  sending: boolean;
  draft: string;
  notice: Notice | null;
  lead: LeadView | null;
  leadOpen: boolean;
  leadOp: LeadOp;
  fieldErrors: Partial<Record<FieldName, string>>;
  formError: string | null;
  debugAvailable: boolean;
  debugOn: boolean;
  engine: string | null;
}

export const initialState: ChatState = {
  phase: "booting",
  epoch: 0,
  session: null,
  messages: [],
  pending: null,
  sending: false,
  draft: "",
  notice: null,
  lead: null,
  leadOpen: false,
  leadOp: null,
  fieldErrors: {},
  formError: null,
  debugAvailable: false,
  debugOn: false,
  engine: null,
};

export type Action =
  | { type: "booted"; engine: string; debugAvailable: boolean }
  | { type: "fatal"; text: string }
  | { type: "session_started"; session: SessionCredentials; keepDraft?: boolean }
  | { type: "history_loaded"; epoch: number; messages: MessageView[]; lead: LeadView | null }
  | { type: "draft_changed"; text: string }
  | { type: "restore"; pending: PendingTurn | null; draft: string }
  | { type: "send_started"; pending: PendingTurn }
  | { type: "send_succeeded"; epoch: number; requestId: string; messages: MessageView[]; lead: LeadView | null; debugAvailable: boolean }
  | { type: "send_failed"; epoch: number; requestId: string; notice: Notice; dropPending: boolean; messages?: MessageView[] }
  | { type: "lead_opened" }
  | { type: "lead_op_started"; op: Exclude<LeadOp, null> }
  | { type: "lead_op_succeeded"; epoch: number; lead: LeadView }
  | { type: "lead_op_failed"; epoch: number; field: FieldName | null; text: string }
  | { type: "field_edited"; field: FieldName }
  | { type: "notice_cleared" }
  | { type: "debug_toggled" };

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "booted":
      return { ...state, engine: action.engine, debugAvailable: action.debugAvailable };
    case "fatal":
      return { ...state, phase: "fatal", sending: false, notice: { tone: "error", text: action.text } };
    case "session_started":
      return {
        ...state,
        phase: "ready",
        epoch: state.epoch + 1,
        session: action.session,
        messages: [],
        pending: null,
        sending: false,
        draft: action.keepDraft ? state.draft : "",
        notice: null,
        lead: null,
        leadOpen: false,
        leadOp: null,
        fieldErrors: {},
        formError: null,
      };
    case "restore":
      return { ...state, pending: action.pending, draft: action.pending?.text ?? action.draft };
    case "history_loaded":
      if (action.epoch !== state.epoch) return state; // устаревший ответ
      return { ...state, phase: "ready", messages: action.messages, lead: action.lead, leadOpen: state.leadOpen || isLeadVisible(action.lead) };
    case "draft_changed":
      return { ...state, draft: action.text };
    case "send_started":
      return { ...state, sending: true, pending: action.pending, notice: null };
    case "send_succeeded":
      if (action.epoch !== state.epoch || state.pending?.request_id !== action.requestId) return state;
      return {
        ...state,
        sending: false,
        pending: null,
        draft: "",
        messages: action.messages,
        lead: action.lead ?? state.lead,
        leadOpen: state.leadOpen || isLeadVisible(action.lead),
        debugAvailable: state.debugAvailable || action.debugAvailable,
      };
    case "send_failed":
      if (action.epoch !== state.epoch || state.pending?.request_id !== action.requestId) return state;
      return {
        ...state,
        sending: false,
        pending: action.dropPending ? null : state.pending,
        messages: action.messages ?? state.messages,
        notice: action.notice,
      };
    case "lead_opened":
      return { ...state, leadOpen: true };
    case "lead_op_started":
      return { ...state, leadOp: action.op, formError: null, ...(action.op === "contacts" ? { fieldErrors: {} } : {}) };
    case "lead_op_succeeded":
      if (action.epoch !== state.epoch) return state;
      return { ...state, leadOp: null, lead: action.lead, leadOpen: true, fieldErrors: {}, formError: null };
    case "lead_op_failed":
      if (action.epoch !== state.epoch) return state;
      return action.field
        ? { ...state, leadOp: null, fieldErrors: { ...state.fieldErrors, [action.field]: action.text } }
        : { ...state, leadOp: null, formError: action.text };
    case "field_edited": {
      const { [action.field]: _removed, ...rest } = state.fieldErrors;
      return { ...state, fieldErrors: rest };
    }
    case "notice_cleared":
      return { ...state, notice: null };
    case "debug_toggled":
      return { ...state, debugOn: !state.debugOn };
  }
}

export function isLeadVisible(lead: LeadView | null | undefined): boolean {
  return Boolean(lead && (lead.status !== "none" || lead.contact_refused));
}

/** Что показать в ленте, пока ход в пути: реплика клиента сразу, без ожидания сервера. */
export function visibleMessages(state: ChatState): MessageView[] {
  const p = state.pending;
  if (!p || state.messages.some((m) => m.request_id === p.request_id && m.role === "user")) return state.messages;
  return [
    ...state.messages,
    { id: -1, seq: state.messages.length + 1, role: "user", content: p.text, request_id: p.request_id, in_context: false, created_at: "" },
  ];
}
