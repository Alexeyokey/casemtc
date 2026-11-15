import type {
  ContactsUpdate,
  Health,
  LeadSaveRequest,
  LeadView,
  SessionCreated,
  SessionCredentials,
  SessionView,
  TraceView,
  TurnRequest,
  TurnResponse,
} from "./types";

/**
 * Единственная точка, через которую интерфейс говорит с сервером.
 * Компоненты и состояние не знают ни URL, ни заголовков, ни fetch.
 */
export interface ConsultationApi {
  health(): Promise<Health>;
  createSession(previous?: SessionCredentials): Promise<SessionCreated>;
  getSession(s: SessionCredentials): Promise<SessionView>;
  sendMessage(s: SessionCredentials, turn: TurnRequest): Promise<TurnResponse>;
  cancelTurn(s: SessionCredentials, requestId: string): Promise<{ status: string }>;
  getTrace(s: SessionCredentials, requestId: string): Promise<TraceView>;
  getLead(s: SessionCredentials): Promise<LeadView>;
  updateContacts(s: SessionCredentials, update: ContactsUpdate): Promise<LeadView>;
  refuseContacts(s: SessionCredentials): Promise<LeadView>;
  saveLead(s: SessionCredentials, request: LeadSaveRequest): Promise<LeadView>;
}

/** Ошибка API в одном виде для HTTP и mock. status 0 — сеть недоступна. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly field: "phone" | "full_name" | "company" | null;

  constructor(code: string, message: string, status: number, retryable = false) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.field = fieldFor(code, message);
  }

  get isNetwork(): boolean {
    return this.status === 0;
  }
}

function fieldFor(code: string, message: string): ApiError["field"] {
  if (code === "invalid_phone" || code === "phone_required") return "phone";
  if (code === "validation_error") {
    if (/phone/.test(message)) return "phone";
    if (/full_name/.test(message)) return "full_name";
    if (/company/.test(message)) return "company";
  }
  return null;
}

export function networkError(): ApiError {
  return new ApiError(
    "network",
    "Нет связи с сервером. История сохранена — повторите отправку, когда связь появится.",
    0,
    true,
  );
}

/** request_id и ключи идемпотентности: 32 hex-символа. */
export function newRequestId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
