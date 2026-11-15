import fixtures from "../fixtures/consultation.json";
import { normalizePhone } from "../lib/phone";
import { ApiError, newRequestId, type ConsultationApi } from "./adapter";
import type {
  ActionType,
  ContactInfo,
  Evidence,
  LeadSummary,
  LeadView,
  MessageView,
  SessionCreated,
  SessionCredentials,
  StageTiming,
  TraceView,
  TurnResponse,
} from "./types";

/**
 * Mock-реализация того же контракта на синтетических фикстурах.
 * Повторяет поведение backend там, где от него зависит интерфейс:
 * идемпотентность request_id и сохранения, invalid_phone, stale_offer,
 * закрытые сессии, ошибку engine с повтором (маркер «#сбой» в тексте).
 */
interface MockSession {
  creds: SessionCredentials;
  status: "active" | "closed";
  created_at: string;
  messages: MessageView[];
  turns: Map<string, { text: string; response?: TurnResponse; failedOnce?: boolean }>;
  traces: Map<string, TraceView>;
  contact: ContactInfo;
  callback: boolean;
  refused: boolean;
  facts: string[];
  topic: string | null;
  lead: null | { lead_id: string; saved: boolean; revision: number; saved_offer: string | null; keys: Set<string>; saved_at: string | null };
}

export interface MockOptions {
  latencyMs?: number;
}

const EMPTY_CONTACT: ContactInfo = { full_name: null, phone: null, company: null, region: null };

function hex64(input: string): string {
  // Детерминированный псевдо-хеш: offer_id меняется вместе с контактами и резюме.
  let h1 = 0x811c9dc5;
  let out = "";
  for (let round = 0; out.length < 64; round++) {
    for (let i = 0; i < input.length; i++) {
      h1 ^= input.charCodeAt(i) + round;
      h1 = Math.imul(h1, 0x01000193) >>> 0;
    }
    out += h1.toString(16).padStart(8, "0");
  }
  return out.slice(0, 64);
}

const includesAny = (text: string, words: string[]) => words.some((w) => text.includes(w));

export function createMockApi({ latencyMs = 700 }: MockOptions = {}): ConsultationApi {
  const sessions = new Map<string, MockSession>();
  let messageId = 0;
  const wait = () => new Promise((r) => setTimeout(r, latencyMs));
  const now = () => new Date().toISOString();

  function auth(c: SessionCredentials): MockSession {
    const s = sessions.get(c.session_id);
    if (!s || s.creds.client_token !== c.client_token) {
      throw new ApiError("session_not_found", "Диалог не найден. Начните новый диалог.", 404);
    }
    return s;
  }

  function summary(s: MockSession): LeadSummary {
    const unknowns = [
      !s.contact.full_name && "ФИО",
      !s.contact.company && "Компания",
      !s.contact.region && "Регион",
    ].filter(Boolean) as string[];
    return {
      task: s.topic ?? "Клиент запросил обратную связь",
      known_facts: s.facts,
      assumptions: [],
      discussed_solutions: [],
      unknowns,
      next_step: s.contact.phone ? "Связаться с клиентом по телефону и уточнить неизвестные сведения" : "Контакт для связи не получен",
    };
  }

  function leadView(s: MockSession): LeadView {
    const sum = summary(s);
    const offer = s.contact.phone ? hex64(JSON.stringify([s.contact, sum])) : null;
    const saved = Boolean(s.lead?.saved && s.lead.saved_offer === offer);
    const status = saved ? "saved" : s.contact.phone ? "ready" : s.lead || s.callback ? "draft" : "none";
    const text = s.refused && !s.contact.phone
      ? fixtures.status_text.refused
      : status === "none" ? "Заявка ещё не начата." : fixtures.status_text[status];
    const missing = [!s.contact.phone && "Телефон", !s.contact.full_name && "ФИО", !s.contact.company && "Компания", !s.contact.region && "Регион"]
      .filter(Boolean) as string[];
    return {
      lead_id: s.lead?.lead_id ?? null,
      session_id: s.creds.session_id,
      status,
      contact: { ...s.contact },
      contact_refused: s.refused,
      callback_requested: s.callback,
      missing_fields: missing,
      summary: s.lead || s.callback ? sum : null,
      offer_id: status === "ready" ? offer : saved ? offer : null,
      revision: s.lead?.revision ?? 0,
      saved_at: s.lead?.saved_at ?? null,
      delivery: "stored_only",
      status_text: text,
    };
  }

  function ensureLead(s: MockSession) {
    s.lead ??= { lead_id: `lead_${newRequestId().slice(0, 16)}`, saved: false, revision: 0, saved_offer: null, keys: new Set(), saved_at: null };
  }

  function push(s: MockSession, role: MessageView["role"], content: string, rid: string, inContext = true): MessageView {
    const m: MessageView = { id: ++messageId, seq: s.messages.length + 1, role, content, request_id: rid, in_context: inContext, created_at: now() };
    s.messages.push(m);
    return m;
  }

  function respond(s: MockSession, text: string): { answer: string; action: ActionType; clarifying: string | null; evidence: Evidence[]; used: string[] } {
    const low = text.toLowerCase();
    const phone = normalizePhone(text.match(/\+?\d[\d\s\-()]{8,18}\d/)?.[0]);
    if (phone) s.contact.phone = phone;
    const scale = low.match(/(\d+)\s+(объект\w*|склад\w*|насос\w*|здани\w*)/);
    if (scale) s.facts = [`масштаб: ${scale[1]} ${scale[2]}`];
    if (includesAny(low, fixtures.refusal.keywords)) {
      s.refused = true;
      s.callback = false;
      return { answer: fixtures.refusal.answer, action: "answer", clarifying: null, evidence: [], used: [] };
    }
    if (includesAny(low, fixtures.callback.keywords)) s.callback = true;
    if (s.callback) {
      ensureLead(s);
      return s.contact.phone
        ? { answer: fixtures.callback.answer_ready, action: "lead_ready", clarifying: null, evidence: [], used: [] }
        : { answer: fixtures.callback.answer_need_phone, action: "collect_contacts", clarifying: null, evidence: [], used: [] };
    }
    const topic = fixtures.topics.find((t) => includesAny(low, t.keywords));
    if (!topic) {
      return { answer: fixtures.fallback.answer, action: "clarify", clarifying: fixtures.fallback.clarifying_question, evidence: [], used: [] };
    }
    s.topic ??= text.slice(0, 200);
    const evidence = topic.evidence.map((e) => ({ ...e, text: "" }));
    return { answer: topic.answer, action: topic.action as ActionType, clarifying: null, evidence, used: topic.used };
  }

  return {
    async health() {
      return { status: "ok", contract_version: "1.0.0", engine: fixtures.engine, build_sha: null };
    },

    async createSession(previous): Promise<SessionCreated> {
      if (previous) {
        const old = auth(previous);
        old.status = "closed";
      }
      const creds = { session_id: `s_mock_${newRequestId()}`, client_token: newRequestId() };
      sessions.set(creds.session_id, {
        creds, status: "active", created_at: now(), messages: [], turns: new Map(), traces: new Map(),
        contact: { ...EMPTY_CONTACT }, callback: false, refused: false, facts: [], topic: null, lead: null,
      });
      return { ...creds, contract_version: "1.0.0", status: "active", created_at: now() };
    },

    async getSession(c) {
      const s = auth(c);
      const lead = leadView(s);
      return {
        contract_version: "1.0.0", session_id: c.session_id, status: s.status, created_at: s.created_at,
        closed_at: s.status === "closed" ? now() : null, messages: s.messages.map((m) => ({ ...m })),
        lead: lead.status === "none" && !lead.contact_refused ? null : lead,
      };
    },

    async sendMessage(c, turn) {
      const s = auth(c);
      if (s.status !== "active") throw new ApiError("session_closed", "Этот диалог завершён. Начните новый диалог.", 409);
      const seen = s.turns.get(turn.request_id);
      if (seen?.response) {
        if (seen.text !== turn.text) throw new ApiError("request_id_conflict", "request_id уже использован для другой реплики", 409);
        return { ...seen.response, replayed: true, lead: leadView(s) };
      }
      const started = performance.now();
      const userMsg = seen ? s.messages.find((m) => m.request_id === turn.request_id && m.role === "user")! : push(s, "user", turn.text, turn.request_id, false);
      s.turns.set(turn.request_id, { text: turn.text, failedOnce: seen?.failedOnce });
      await wait();
      if (turn.text.includes("#сбой") && !seen?.failedOnce) {
        s.turns.set(turn.request_id, { text: turn.text, failedOnce: true });
        throw new ApiError("engine_error", "Не удалось подготовить ответ. Повторите отправку — сообщение не потеряется.", 502, true);
      }
      const r = respond(s, turn.text);
      userMsg.in_context = true;
      const asst = push(s, "assistant", r.answer, turn.request_id);
      const total = performance.now() - started;
      const timings: StageTiming[] = [
        { name: "mock.latency", ms: total, status: "ok", error: null },
        { name: "total", ms: total, status: "ok", error: null },
      ];
      s.traces.set(turn.request_id, {
        contract_version: "1.0.0", trace_id: `t_${newRequestId().slice(0, 20)}`, session_id: c.session_id,
        request_id: turn.request_id, attempt: seen ? 2 : 1, status: "completed", engine: fixtures.engine, created_at: now(),
        timings, retrieved: r.evidence, used: r.evidence.filter((e) => r.used.includes(e.evidence_id)), used_missing_ids: [],
        action: r.action, error: null,
      });
      const lead = leadView(s);
      const response: TurnResponse = {
        contract_version: "1.0.0", session_id: c.session_id, request_id: turn.request_id, status: "completed",
        user_message_id: userMsg.id, assistant_message_id: asst.id, answer: r.answer, action: r.action,
        clarifying_question: r.clarifying, lead: lead.status === "none" && !lead.contact_refused ? null : lead,
        debug_available: true, replayed: false,
      };
      s.turns.set(turn.request_id, { text: turn.text, response });
      return response;
    },

    async cancelTurn(c, rid) {
      auth(c);
      return { status: rid ? "cancelled" : "unknown" };
    },

    async getTrace(c, rid) {
      const t = auth(c).traces.get(rid);
      if (!t) throw new ApiError("trace_not_found", "Для этого ответа нет отладочной трассы", 404);
      return t;
    },

    async getLead(c) {
      return leadView(auth(c));
    },

    async updateContacts(c, update) {
      const s = auth(c);
      await wait();
      if (update.phone !== undefined && update.phone !== null && update.phone !== "") {
        const p = normalizePhone(update.phone);
        if (!p) throw new ApiError("invalid_phone", "Номер телефона не распознан. Пример: +7 916 123-45-67.", 422);
        s.contact.phone = p;
      } else if (update.phone !== undefined) {
        s.contact.phone = null;
      }
      for (const k of ["full_name", "company", "region"] as const) {
        if (update[k] !== undefined) s.contact[k] = update[k]?.trim() || null;
      }
      if (update.callback_requested) s.callback = true;
      if (s.contact.phone) s.refused = false;
      ensureLead(s);
      return leadView(s);
    },

    async refuseContacts(c) {
      const s = auth(c);
      s.refused = true;
      s.callback = false;
      return leadView(s);
    },

    async saveLead(c, req) {
      const s = auth(c);
      await wait();
      const view = leadView(s);
      if (s.lead?.keys.has(req.idempotency_key) && s.lead.saved) return leadView(s);
      if (!s.contact.phone) {
        throw new ApiError("phone_required", "Чтобы сохранить заявку, укажите телефон для связи.", 422);
      }
      if (view.offer_id !== req.offer_id) {
        throw new ApiError("stale_offer", "Данные заявки изменились. Проверьте их и подтвердите ещё раз.", 409);
      }
      ensureLead(s);
      const lead = s.lead!;
      if (!(lead.saved && lead.saved_offer === req.offer_id)) {
        lead.saved = true;
        lead.revision += 1;
        lead.saved_offer = req.offer_id;
        lead.saved_at = now();
      }
      lead.keys.add(req.idempotency_key);
      return leadView(s);
    },
  };
}
