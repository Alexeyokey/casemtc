import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { ApiError, newRequestId, type ConsultationApi } from "../api/adapter";
import type { ContactsUpdate, LeadView, SessionCredentials, TraceView } from "../api/types";
import { initialState, reducer, type FieldName, type PendingTurn } from "./chatState";
import { loadPersisted, savePersisted } from "./storage";

/**
 * Оркестратор диалога. Три гарантии, на которые опирается интерфейс:
 *  1. Дубли: синхронный замок (ref) + один request_id на реплику; повтор
 *     после обрыва идёт с тем же request_id, сервер вернёт сохранённый ответ.
 *  2. Устаревшие ответы: каждое действие запоминает epoch сессии; если за время
 *     запроса начат новый диалог, результат отбрасывается (и в reducer тоже).
 *  3. Ничего не теряется: реплика в пути и черновик лежат в sessionStorage,
 *     при ошибке переписка перечитывается с сервера, текст остаётся в поле.
 */
export function useConsultation(api: ConsultationApi) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const live = useRef(state);
  live.current = state;
  const sendLock = useRef(false);
  // epoch дублируется в ref: он нужен синхронно, до следующей перерисовки.
  const epochRef = useRef(0);
  const begin = (session: SessionCredentials, keepDraft = false) => {
    epochRef.current += 1;
    dispatch({ type: "session_started", session, keepDraft });
    return epochRef.current;
  };
  const leadLock = useRef(false);
  const saveKeys = useRef(new Map<string, string>());

  // Сохраняем вкладку при каждом изменении того, что нельзя потерять.
  useEffect(() => {
    if (state.phase === "booting") return;
    savePersisted({ session: state.session, pending: state.pending, draft: state.draft });
  }, [state.phase, state.session, state.pending, state.draft]);

  const loadHistory = useCallback(
    async (session: SessionCredentials, epoch: number) => {
      const view = await api.getSession(session);
      dispatch({ type: "history_loaded", epoch, messages: view.messages, lead: view.lead });
      return view;
    },
    [api],
  );

  const startSession = useCallback(
    async (closePrevious: boolean) => {
      const previous = live.current.session;
      const created = await api.createSession(closePrevious && previous ? previous : undefined);
      const session = { session_id: created.session_id, client_token: created.client_token };
      saveKeys.current.clear();
      begin(session);
      return session;
    },
    [api],
  );

  // Старт: health → восстановление сессии вкладки или новая.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const health = await api.health();
        if (cancelled) return;
        dispatch({ type: "booted", engine: health.engine, debugAvailable: false });
        const saved = loadPersisted();
        if (saved.session) {
          const epoch = begin(saved.session, true);
          dispatch({ type: "restore", pending: saved.pending, draft: saved.draft });
          try {
            await loadHistory(saved.session, epoch);
            if (saved.pending) {
              sendLock.current = false;
              dispatch({ type: "send_failed", epoch, requestId: saved.pending.request_id, dropPending: false,
                notice: { tone: "info", text: "Последнее сообщение могло не дойти. Повторите отправку — дубля не будет.", canRetry: true } });
            }
            return;
          } catch (e) {
            if (!(e instanceof ApiError) || e.status !== 404) throw e;
          }
        }
        if (cancelled) return;
        await startSession(false);
        if (saved.draft) dispatch({ type: "draft_changed", text: saved.draft });
      } catch (e) {
        if (!cancelled) dispatch({ type: "fatal", text: messageOf(e, "Не удалось открыть чат. Обновите страницу.") });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, loadHistory, startSession]);

  const runTurn = useCallback(async (explicit?: PendingTurn) => {
    const session = live.current.session;
    const pending = explicit ?? live.current.pending;
    const epoch = epochRef.current;
    if (!session || !pending) {
      sendLock.current = false;
      return;
    }
    sendLock.current = true;
    dispatch({ type: "send_started", pending });
    try {
      const res = await api.sendMessage(session, pending);
      if (epochRef.current !== epoch) return;
      let messages = live.current.messages;
      try {
        messages = (await api.getSession(session)).messages;
      } catch {
        messages = [
          ...messages,
          { id: res.user_message_id, seq: messages.length + 1, role: "user", content: pending.text, request_id: pending.request_id, in_context: true, created_at: "" },
          { id: res.assistant_message_id, seq: messages.length + 2, role: "assistant", content: res.answer, request_id: pending.request_id, in_context: true, created_at: "" },
        ];
      }
      dispatch({ type: "send_succeeded", epoch, requestId: pending.request_id, messages, lead: res.lead, debugAvailable: res.debug_available });
    } catch (e) {
      if (epochRef.current !== epoch) return;
      const err = e instanceof ApiError ? e : null;
      // Повторять имеет смысл сеть, 5xx и «ещё готовится»; остальное — сбросить реплику, текст оставить в поле.
      const retryable = !err || err.isNetwork || err.retryable;
      let messages;
      try {
        messages = (await api.getSession(session)).messages;
      } catch {
        /* показываем то, что было */
      }
      dispatch({
        type: "send_failed",
        epoch,
        requestId: pending.request_id,
        dropPending: !retryable,
        messages,
        notice: {
          tone: "error",
          text: err?.isNetwork
            ? "Связь прервалась. Переписка сохранена — нажмите «Повторить», сообщение не задвоится."
            : messageOf(e, "Не удалось получить ответ."),
          canRetry: retryable,
        },
      });
      if (err?.code === "session_closed") await startSession(false).catch(() => undefined);
    } finally {
      if (epochRef.current === epoch) sendLock.current = false;
    }
  }, [api, startSession]);

  const send = useCallback(
    (text: string) => {
      const clean = text.trim();
      const s = live.current;
      if (!clean || sendLock.current || s.sending || s.pending || leadLock.current || !s.session) return;
      sendLock.current = true;
      void runTurn({ request_id: newRequestId(), text: clean });
    },
    [runTurn],
  );

  const retry = useCallback(() => {
    const s = live.current;
    if (!s.pending || s.sending || sendLock.current) return;
    void runTurn();
  }, [runTurn]);

  // Новый диалог можно начать и во время ожидания: ответ старой сессии
  // будет отброшен по epoch, а backend при закрытии отменит её незавершённый ход.
  const newDialog = useCallback(async () => {
    sendLock.current = false;
    leadLock.current = false;
    try {
      await startSession(true);
    } catch (e) {
      dispatch({ type: "fatal", text: messageOf(e, "Не удалось начать новый диалог.") });
    }
  }, [startSession]);

  const leadOp = useCallback(
    async (op: "contacts" | "save" | "refuse", run: (s: SessionCredentials) => Promise<LeadView>) => {
      const session = live.current.session;
      const epoch = epochRef.current;
      if (!session || leadLock.current || live.current.sending) return;
      leadLock.current = true;
      dispatch({ type: "lead_op_started", op });
      try {
        const lead = await run(session);
        dispatch({ type: "lead_op_succeeded", epoch, lead });
      } catch (e) {
        const err = e instanceof ApiError ? e : null;
        if (err?.code === "stale_offer") {
          await loadHistory(session, epoch).catch(() => undefined);
        }
        dispatch({ type: "lead_op_failed", epoch, field: (err?.field as FieldName | null) ?? null,
          text: messageOf(e, "Не удалось обновить заявку. Повторите попытку.") });
      } finally {
        leadLock.current = false;
      }
    },
    [loadHistory],
  );

  const actions = useMemo(
    () => ({
      send,
      retry,
      newDialog,
      setDraft: (text: string) => dispatch({ type: "draft_changed", text }),
      dismissNotice: () => dispatch({ type: "notice_cleared" }),
      toggleDebug: () => dispatch({ type: "debug_toggled" }),
      openLead: () => {
        dispatch({ type: "lead_opened" });
        // Форма уже открыта — просто показываем её (requestAnimationFrame: после отрисовки).
        requestAnimationFrame(() =>
          document.getElementById("lead-title")?.scrollIntoView({ block: "center", behavior: "smooth" }),
        );
        // Явная просьба о связи: заводим черновик заявки, если его ещё нет.
        if (!live.current.lead || live.current.lead.status === "none") {
          void leadOp("contacts", (s) => api.updateContacts(s, { callback_requested: true }));
        }
      },
      submitContacts: (update: ContactsUpdate) =>
        leadOp("contacts", (s) => api.updateContacts(s, { ...update, callback_requested: true })),
      refuse: () => leadOp("refuse", (s) => api.refuseContacts(s)),
      saveLead: () => {
        const offer = live.current.lead?.offer_id;
        if (!offer) return;
        if (!saveKeys.current.has(offer)) saveKeys.current.set(offer, newRequestId());
        const key = saveKeys.current.get(offer)!;
        return leadOp("save", (s) => api.saveLead(s, { offer_id: offer, idempotency_key: key }));
      },
      fieldEdited: (field: FieldName) => dispatch({ type: "field_edited", field }),
      loadTrace: (requestId: string): Promise<TraceView> => {
        const s = live.current.session;
        if (!s) return Promise.reject(new Error("Нет активного диалога"));
        return api.getTrace(s, requestId);
      },
    }),
    [api, send, retry, newDialog, leadOp],
  );

  return { state, actions };
}

function messageOf(e: unknown, fallback: string): string {
  return e instanceof ApiError || e instanceof Error ? e.message || fallback : fallback;
}
