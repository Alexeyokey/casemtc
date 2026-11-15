import { useState } from "react";
import type { Evidence, TraceView } from "../api/types";
import styles from "./DebugDisclosure.module.css";

/**
 * Отладка выбранного ответа. Показываем только белый список полей:
 * статус, действие, попытку, привязку к реплике, документы и время этапов.
 * Тексты фрагментов БЗ, токены и внутренние идентификаторы не выводятся.
 */
export function DebugDisclosure({ requestId, loadTrace }: { requestId: string; loadTrace: (id: string) => Promise<TraceView> }) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<TraceView | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !trace) {
      setError(null);
      try {
        setTrace(await loadTrace(requestId));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Не удалось загрузить данные отладки.");
      }
    }
  }

  const panelId = `debug-${requestId}`;
  return (
    <div className={styles.wrap}>
      <button type="button" className={styles.toggle} aria-expanded={open} aria-controls={panelId} onClick={toggle}>
        {open ? "Скрыть источники и статус" : "Источники и статус"}
      </button>
      {open && (
        <div id={panelId} className={styles.panel}>
          {error && <p className={styles.error}>{error}</p>}
          {!error && !trace && <p className={styles.muted}>Загружаем…</p>}
          {trace && <TraceBody trace={trace} />}
        </div>
      )}
    </div>
  );
}

function docLabel(e: Evidence): string {
  const title = e.doc_title || "Документ без названия";
  return e.section ? `${title}, раздел «${e.section}»` : title;
}

function TraceBody({ trace }: { trace: TraceView }) {
  const usedIds = new Set(trace.used.map((e) => e.evidence_id));
  const otherFound = trace.retrieved.filter((e) => !usedIds.has(e.evidence_id));
  const statusText = { completed: "ответ сохранён", failed: "ошибка этапа", cancelled: "отменён" }[trace.status];
  return (
    <dl className={styles.grid}>
      <dt>Статус</dt>
      <dd>
        {statusText}
        {trace.action ? `, действие: ${actionLabel(trace.action)}` : ""}
        {trace.attempt > 1 ? `, попытка ${trace.attempt}` : ""}
      </dd>
      <dt>Ответ на реплику</dt>
      <dd className={styles.mono}>#{trace.request_id.slice(0, 8)}</dd>
      <dt>Использовано</dt>
      <dd>
        {trace.used.length ? (
          <ul className={styles.docs}>
            {trace.used.map((e) => (
              <li key={e.evidence_id}>{docLabel(e)}</li>
            ))}
          </ul>
        ) : (
          "Ответ без опоры на документы"
        )}
        {trace.used_missing_ids.length > 0 && (
          <p className={styles.error}>Ответ ссылается на {trace.used_missing_ids.length} фрагм. вне найденных — проверьте поиск.</p>
        )}
      </dd>
      {otherFound.length > 0 && (
        <>
          <dt>Найдено, но не использовано</dt>
          <dd>
            <ul className={styles.docs}>
              {otherFound.map((e) => (
                <li key={e.evidence_id}>{docLabel(e)}</li>
              ))}
            </ul>
          </dd>
        </>
      )}
      <dt>Время этапов</dt>
      <dd>
        <ul className={styles.timings}>
          {trace.timings.map((t, i) => (
            <li key={`${t.name}-${i}`} data-error={t.status === "error" || undefined}>
              <span>{stageLabel(t.name)}</span>
              <span className={styles.num}>{Math.round(t.ms).toLocaleString("ru-RU")} мс</span>
            </li>
          ))}
        </ul>
      </dd>
      {trace.error && (
        <>
          <dt>Ошибка</dt>
          <dd className={styles.error}>
            {trace.error.stage ? `Этап «${stageLabel(trace.error.stage)}»` : "Этап не указан"}
            {trace.error.message ? `: ${trace.error.message}` : ""}
          </dd>
        </>
      )}
    </dl>
  );
}

const STAGES: Record<string, string> = {
  "engine.retrieval": "поиск",
  "engine.generation": "генерация",
  "engine.engine_total": "консультант целиком",
  backend_prepare: "подготовка",
  engine: "вызов консультанта",
  backend_persist: "сохранение",
  total: "всего",
  "mock.latency": "mock-задержка",
};

function stageLabel(name: string): string {
  return STAGES[name] ?? name.replace(/^engine\./, "");
}

function actionLabel(a: string): string {
  return (
    {
      answer: "ответ",
      clarify: "уточнение",
      collect_contacts: "сбор контактов",
      lead_ready: "заявка готова",
      escalate: "передача специалисту",
      out_of_scope: "вне компетенции",
    }[a] ?? a
  );
}
