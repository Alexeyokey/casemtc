import { useEffect, useId, useRef, useState } from "react";
import type { ContactsUpdate, LeadView } from "../api/types";
import type { FieldName, LeadOp } from "../state/chatState";
import { formatPhone, normalizePhone, PHONE_HINT } from "../lib/phone";
import { Icon } from "./Icon";
import styles from "./LeadPanel.module.css";

interface Props {
  lead: LeadView | null;
  busy: LeadOp;
  chatBusy: boolean;
  fieldErrors: Partial<Record<FieldName, string>>;
  formError: string | null;
  onSubmit: (update: ContactsUpdate) => void;
  onSave: () => void;
  onRefuse: () => void;
  onFieldEdited: (field: FieldName) => void;
}

type Values = Record<FieldName, string>;

const fromLead = (lead: LeadView | null): Values => ({
  phone: formatPhone(lead?.contact.phone) || "",
  full_name: lead?.contact.full_name ?? "",
  company: lead?.contact.company ?? "",
});

/**
 * Заявка внутри чата. Один основной шаг за раз:
 * «Проверить данные» → сервер нормализует контакты и пересобирает резюме →
 * «Сохранить заявку». Любая правка полей возвращает к проверке, поэтому
 * сохраняется ровно то, что пользователь видел в предпросмотре.
 */
export function LeadPanel({ lead, busy, chatBusy, fieldErrors, formError, onSubmit, onSave, onRefuse, onFieldEdited }: Props) {
  const [values, setValues] = useState<Values>(() => fromLead(lead));
  const [dirty, setDirty] = useState(false);
  const [localPhoneError, setLocalPhoneError] = useState<string | null>(null);
  const [editingSaved, setEditingSaved] = useState(false);
  const [reopened, setReopened] = useState(false);
  const ids = { phone: useId(), full_name: useId(), company: useId() };
  const headingRef = useRef<HTMLHeadingElement>(null);
  const phoneRef = useRef<HTMLInputElement>(null);

  // Когда сервер подтвердил контакты — показываем их в его нормализованном виде.
  useEffect(() => {
    if (!dirty) setValues(fromLead(lead));
  }, [lead, dirty]);

  // Ошибка телефона от сервера → фокус в поле, чтобы исправить сразу.
  useEffect(() => {
    if (fieldErrors.phone) phoneRef.current?.focus();
  }, [fieldErrors.phone]);

  useEffect(() => {
    headingRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, []);

  const status = lead?.status ?? "none";
  const saved = status === "saved" && !editingSaved;
  const refused = Boolean(lead?.contact_refused) && !lead?.contact.phone && !reopened;
  const disabled = Boolean(busy) || chatBusy;
  const canSave = status === "ready" && Boolean(lead?.offer_id) && !dirty;
  const phoneError = localPhoneError ?? fieldErrors.phone ?? null;

  function edit(field: FieldName, value: string) {
    setValues((v) => ({ ...v, [field]: value }));
    setDirty(true);
    if (field === "phone") setLocalPhoneError(null);
    onFieldEdited(field);
  }

  function check(e: React.FormEvent) {
    e.preventDefault();
    if (disabled) return;
    if (!values.phone.trim()) {
      setLocalPhoneError("Укажите телефон — без него специалист не сможет связаться.");
      phoneRef.current?.focus();
      return;
    }
    if (!normalizePhone(values.phone)) {
      setLocalPhoneError(`Номер не распознан. ${PHONE_HINT}.`);
      phoneRef.current?.focus();
      return;
    }
    setDirty(false);
    setEditingSaved(false);
    onSubmit({ phone: values.phone, full_name: values.full_name.trim() || null, company: values.company.trim() || null });
  }

  if (refused) {
    return (
      <section className={styles.card} aria-labelledby="lead-title">
        <h2 id="lead-title" ref={headingRef} className={styles.title}>Без заявки</h2>
        <p className={styles.status}>{lead?.status_text}</p>
        <button type="button" className={styles.secondary} onClick={() => setReopened(true)} disabled={disabled}>
          <Icon name="phone" size={18} />
          Всё-таки оставить телефон
        </button>
      </section>
    );
  }

  if (saved && lead) {
    return (
      <section className={`${styles.card} ${styles.receipt}`} aria-labelledby="lead-title" aria-live="polite">
        <div className={styles.receiptHead}>
          <span className={styles.badge}>
            <Icon name="check" size={18} />
          </span>
          <div>
            <h2 id="lead-title" ref={headingRef} className={styles.title}>Заявка сохранена</h2>
            <p className={styles.status}>{lead.status_text}</p>
          </div>
        </div>
        <dl className={styles.facts}>
          <dt>Телефон</dt>
          <dd className={styles.num}>{formatPhone(lead.contact.phone)}</dd>
          <dt>ФИО</dt>
          <dd>{lead.contact.full_name ?? <span className={styles.na}>не указано</span>}</dd>
          <dt>Компания</dt>
          <dd>{lead.contact.company ?? <span className={styles.na}>не указана</span>}</dd>
          {lead.summary && (
            <>
              <dt>Запрос</dt>
              <dd>{lead.summary.task}</dd>
            </>
          )}
          <dt>Версия</dt>
          <dd className={styles.num}>
            {lead.revision}
            {lead.saved_at ? `, ${new Date(lead.saved_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })}` : ""}
          </dd>
        </dl>
        <button type="button" className={styles.link} onClick={() => setEditingSaved(true)} disabled={disabled}>
          Изменить контакты
        </button>
      </section>
    );
  }

  return (
    <section className={styles.card} aria-labelledby="lead-title">
      <h2 id="lead-title" ref={headingRef} className={styles.title}>Заявка специалисту</h2>
      <p className={styles.status}>
        {lead && status !== "none" ? lead.status_text : "Оставьте телефон — специалист уточнит детали. ФИО и компания по желанию."}
      </p>

      <form className={styles.form} onSubmit={check} noValidate>
        <Field
          id={ids.phone}
          label="Телефон"
          required
          inputRef={phoneRef}
          type="tel"
          autoComplete="tel"
          inputMode="tel"
          placeholder={PHONE_HINT.replace("Например, ", "")}
          value={values.phone}
          error={phoneError}
          hint="Используйте вымышленный номер на демо-стенде"
          onChange={(v) => edit("phone", v)}
          disabled={disabled}
        />
        <div className={styles.row}>
          <Field id={ids.full_name} label="ФИО" optional autoComplete="name" value={values.full_name}
            error={fieldErrors.full_name ?? null} onChange={(v) => edit("full_name", v)} disabled={disabled} />
          <Field id={ids.company} label="Компания" optional autoComplete="organization" value={values.company}
            error={fieldErrors.company ?? null} onChange={(v) => edit("company", v)} disabled={disabled} />
        </div>

        {canSave && lead?.summary && <Preview lead={lead} />}

        {formError && (
          <p className={styles.formError} role="alert">
            <Icon name="alert" size={16} /> {formError}
          </p>
        )}

        <div className={styles.actions}>
          {canSave ? (
            <button type="button" className={styles.primary} onClick={onSave} disabled={disabled} aria-busy={busy === "save"}>
              {busy === "save" ? "Сохраняем…" : "Сохранить заявку"}
            </button>
          ) : (
            <button type="submit" className={styles.primary} disabled={disabled} aria-busy={busy === "contacts"}>
              {busy === "contacts" ? "Проверяем…" : "Проверить данные"}
            </button>
          )}
          {!lead?.contact.phone && (
            <button type="button" className={styles.link} onClick={onRefuse} disabled={disabled}>
              Продолжить без заявки
            </button>
          )}
        </div>
      </form>
    </section>
  );
}

function Preview({ lead }: { lead: LeadView }) {
  const s = lead.summary!;
  return (
    <div className={styles.preview} aria-label="Что увидит специалист">
      <p className={styles.previewTitle}>Что увидит специалист</p>
      <p className={styles.previewTask}>{s.task}</p>
      {s.known_facts.length > 0 && <p className={styles.previewLine}>Известно: {s.known_facts.join("; ")}</p>}
      {s.unknowns.length > 0 && <p className={styles.previewLine}>Не указано: {s.unknowns.join(", ")}</p>}
      <p className={styles.previewLine}>Дальше: {s.next_step}</p>
    </div>
  );
}

interface FieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  error: string | null;
  disabled: boolean;
  required?: boolean;
  optional?: boolean;
  hint?: string;
  type?: string;
  autoComplete?: string;
  inputMode?: "tel" | "text";
  placeholder?: string;
  inputRef?: React.Ref<HTMLInputElement>;
}

function Field({ id, label, value, onChange, error, disabled, required, optional, hint, type = "text", autoComplete, inputMode, placeholder, inputRef }: FieldProps) {
  const describedBy = [error ? `${id}-error` : null, hint ? `${id}-hint` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className={styles.field} data-invalid={Boolean(error) || undefined}>
      <label htmlFor={id} className={styles.label}>
        {label}
        {required && <span className={styles.req}> — обязательно</span>}
        {optional && <span className={styles.opt}> — необязательно</span>}
      </label>
      <input
        id={id}
        ref={inputRef}
        className={styles.input}
        type={type}
        value={value}
        autoComplete={autoComplete}
        inputMode={inputMode}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={Boolean(error) || undefined}
        aria-required={required || undefined}
        aria-describedby={describedBy}
        disabled={disabled}
      />
      {error ? (
        <p id={`${id}-error`} className={styles.error} role="alert">
          <Icon name="alert" size={14} /> {error}
        </p>
      ) : (
        hint && <p id={`${id}-hint`} className={styles.hint}>{hint}</p>
      )}
    </div>
  );
}
