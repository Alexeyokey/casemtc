import type { SessionCredentials } from "../api/types";
import type { PendingTurn } from "./chatState";

/**
 * Состояние вкладки: токен сессии, неотправленная реплика и черновик.
 * sessionStorage, а не localStorage: у каждой вкладки свой диалог.
 * Хранилище может быть недоступно (приватный режим) — тогда работаем без него.
 */
const KEY = "energytool-web-v1";

export interface Persisted {
  session: SessionCredentials | null;
  pending: PendingTurn | null;
  draft: string;
}

export function loadPersisted(storage: Storage | undefined = safeStorage()): Persisted {
  const empty: Persisted = { session: null, pending: null, draft: "" };
  if (!storage) return empty;
  try {
    const raw = JSON.parse(storage.getItem(KEY) ?? "null");
    if (!raw || typeof raw !== "object") return empty;
    const session = raw.session?.session_id && raw.session?.client_token
      ? { session_id: String(raw.session.session_id), client_token: String(raw.session.client_token) }
      : null;
    const pending = raw.pending?.request_id && raw.pending?.text
      ? { request_id: String(raw.pending.request_id), text: String(raw.pending.text) }
      : null;
    return { session, pending, draft: typeof raw.draft === "string" ? raw.draft : "" };
  } catch {
    return empty;
  }
}

export function savePersisted(value: Persisted, storage: Storage | undefined = safeStorage()): void {
  try {
    storage?.setItem(KEY, JSON.stringify(value));
  } catch {
    // Квота или запрет хранилища — открытая вкладка продолжит работать.
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : undefined;
  } catch {
    return undefined;
  }
}
