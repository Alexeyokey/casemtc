import { useLayoutEffect, useRef } from "react";
import type { Notice } from "../state/chatState";
import { Icon } from "./Icon";
import styles from "./Composer.module.css";

interface Props {
  draft: string;
  onDraft: (text: string) => void;
  onSend: (text: string) => void;
  onRetry: () => void;
  onDismiss: () => void;
  sending: boolean;
  blocked: boolean;
  hasPending: boolean;
  notice: Notice | null;
}

export function Composer({ draft, onDraft, onSend, onRetry, onDismiss, sending, blocked, hasPending, notice }: Props) {
  const area = useRef<HTMLTextAreaElement>(null);

  // Авто-высота поля без скачков: считаем до отрисовки, максимум ~6 строк.
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, [draft]);

  const disabled = sending || blocked || hasPending;
  const submit = () => {
    if (!disabled && draft.trim()) onSend(draft);
  };

  return (
    <div className={styles.dock}>
      <div className={styles.inner}>
        {notice && (
          <div className={styles.notice} data-tone={notice.tone} role={notice.tone === "error" ? "alert" : "status"}>
            <Icon name={notice.tone === "error" ? "alert" : "spark"} size={18} />
            <p>{notice.text}</p>
            <div className={styles.noticeActions}>
              {notice.canRetry && hasPending && (
                <button type="button" className={styles.retry} onClick={onRetry} disabled={sending}>
                  <Icon name="retry" size={16} />
                  Повторить
                </button>
              )}
              {!hasPending && (
                <button type="button" className={styles.close} onClick={onDismiss} aria-label="Скрыть сообщение">
                  ×
                </button>
              )}
            </div>
          </div>
        )}
        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label htmlFor="composer" className="visually-hidden">
            Сообщение консультанту
          </label>
          <textarea
            id="composer"
            ref={area}
            className={styles.input}
            rows={1}
            value={draft}
            maxLength={10000}
            placeholder={hasPending ? "Сообщение ждёт повторной отправки" : "Ваш вопрос"}
            onChange={(e) => onDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            readOnly={hasPending}
            aria-describedby="composer-hint"
          />
          <button type="submit" className={styles.send} disabled={disabled || !draft.trim()} aria-busy={sending}>
            {sending ? <span className={styles.spinner} aria-hidden="true" /> : <Icon name="send" size={20} />}
            <span className={styles.sendLabel}>{sending ? "Отправляем" : "Отправить"}</span>
          </button>
        </form>
        <p id="composer-hint" className={styles.hint}>
          Enter — отправить, Shift+Enter — новая строка. Демо-стенд: не вводите реальные данные.
        </p>
      </div>
    </div>
  );
}
