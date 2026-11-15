import styles from "./EmptyState.module.css";

interface Props {
  suggestions: string[];
  onPick: (text: string) => void;
  disabled: boolean;
}

export function EmptyState({ suggestions, onPick, disabled }: Props) {
  return (
    <section className={styles.wrap} aria-labelledby="empty-title">
      <h1 id="empty-title" className={styles.title}>
        Что нужно измерять или&nbsp;контролировать?
      </h1>
      <p className={styles.lead}>
        Опишите объект и задачу своими словами — подберу решение по базе знаний EnergyTool. Контакты понадобятся,
        только если захотите поговорить со специалистом.
      </p>
      <ul className={styles.list} aria-label="Примеры вопросов">
        {suggestions.map((s) => (
          <li key={s}>
            <button type="button" className={styles.suggestion} onClick={() => onPick(s)} disabled={disabled}>
              {s}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
