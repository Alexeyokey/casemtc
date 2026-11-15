import { ApiError, networkError, type ConsultationApi } from "./adapter";
import type { ErrorBody, SessionCredentials } from "./types";

type Method = "GET" | "POST" | "PUT";

/** Адаптер к настоящему backend (`/api/v1`). baseUrl берётся из конфига. */
export function createHttpApi(baseUrl: string, fetchImpl: typeof fetch = fetch.bind(globalThis)): ConsultationApi {
  async function call<T>(path: string, method: Method = "GET", body?: unknown, token?: string): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (token) headers["X-Client-Token"] = token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}/api/v1${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
      });
    } catch {
      throw networkError();
    }
    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      // Тело может отсутствовать (прокси, 502 от балансировщика).
    }
    if (!response.ok) {
      const err = (data as { error?: ErrorBody } | null)?.error;
      throw new ApiError(
        err?.code ?? `http_${response.status}`,
        err?.message ?? "Сервер не смог обработать запрос. Повторите попытку.",
        response.status,
        err?.retryable ?? response.status >= 500,
      );
    }
    return data as T;
  }

  const s = (c: SessionCredentials, tail = "") => `/sessions/${encodeURIComponent(c.session_id)}${tail}`;

  return {
    health: () => call("/health"),
    createSession: (previous) =>
      call("/sessions", "POST", previous ? { close_session_id: previous.session_id } : {}, previous?.client_token),
    getSession: (c) => call(s(c), "GET", undefined, c.client_token),
    sendMessage: (c, turn) => call(s(c, "/messages"), "POST", turn, c.client_token),
    cancelTurn: (c, rid) => call(s(c, `/turns/${encodeURIComponent(rid)}/cancel`), "POST", undefined, c.client_token),
    getTrace: (c, rid) => call(s(c, `/turns/${encodeURIComponent(rid)}/debug`), "GET", undefined, c.client_token),
    getLead: (c) => call(s(c, "/lead"), "GET", undefined, c.client_token),
    updateContacts: (c, update) => call(s(c, "/lead/contacts"), "PUT", update, c.client_token),
    refuseContacts: (c) => call(s(c, "/lead/refuse"), "POST", undefined, c.client_token),
    saveLead: (c, req) => call(s(c, "/lead/save"), "POST", req, c.client_token),
  };
}
