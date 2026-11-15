import { useEffect, useRef } from "react";
import type { MessageView, TraceView } from "../api/types";
import { DebugDisclosure } from "./DebugDisclosure";
import styles from "./MessageList.module.css";

interface Props {
  messages: MessageView[];
  waiting: boolean;
  debugOn: boolean;
  loadTrace: (requestId: string) => Promise<TraceView>;
  children?: React.ReactNode;
}

export function MessageList({ messages, waiting, debugOn, loadTrace, children }: Props) {
  const end = useRef<HTMLDivElement>(null);
  const count = messages.length;

  // Прокрутка к новому сообщению, но только если пользователь и так был внизу.
  useEffect(() => {
    const el = end.current;
    if (!el) return;
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 240;
    if (nearBottom || count <= 2) el.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [count, waiting]);

  return (
    <div className={styles.feed}>
      <ol className={styles.list} aria-label="Переписка" aria-live="polite" aria-relevant="additions">
        {messages.map((m) => (
          <li key={`${m.request_id}-${m.role}`} className={m.role === "user" ? styles.userRow : styles.assistantRow}>
            {m.role === "user" ? (
              <div className={styles.user} data-pending={m.id < 0 || undefined}>
                <span className="visually-hidden">Вы: </span>
                <p className={styles.text}>{m.content}</p>
              </div>
            ) : (
              <article className={styles.assistant} data-interrupted={!m.in_context || undefined}>
                <p className={styles.author}>Консультант</p>
                <p className={styles.text}>{m.content}</p>
                {!m.in_context && <p className={styles.note}>Ответ пришёл после отмены и не учитывается в диалоге.</p>}
                {debugOn && m.in_context && <DebugDisclosure requestId={m.request_id} loadTrace={loadTrace} />}
              </article>
            )}
          </li>
        ))}
        {waiting && (
          <li className={styles.assistantRow}>
            <div className={styles.assistant} role="status">
              <p className={styles.author}>Консультант</p>
              <span className={styles.typing} aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <span className="visually-hidden">Готовим ответ</span>
            </div>
          </li>
        )}
      </ol>
      {children}
      <div ref={end} />
    </div>
  );
}
