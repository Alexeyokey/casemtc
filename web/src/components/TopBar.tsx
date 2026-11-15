import { Icon } from "./Icon";
import styles from "./TopBar.module.css";

interface Props {
  showDebugToggle: boolean;
  debugOn: boolean;
  onToggleDebug: () => void;
  onLead: () => void;
  onNewDialog: () => void;
  busy: boolean;
  booting: boolean;
  leadSaved: boolean;
}

export function TopBar({ showDebugToggle, debugOn, onToggleDebug, onLead, onNewDialog, busy, booting, leadSaved }: Props) {
  return (
    <header className={styles.bar}>
      <div className={styles.inner}>
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true" />
          <div>
            <p className={styles.title}>
              <span className={styles.long}>Консультант </span>EnergyTool
            </p>
            <p className={styles.subtitle}>Учёт энергии и контроль оборудования</p>
          </div>
        </div>
        <div className={styles.actions}>
          {showDebugToggle && (
            <label className={styles.debug}>
              <input type="checkbox" role="switch" checked={debugOn} onChange={onToggleDebug} aria-checked={debugOn} />
              <span>Отладка</span>
            </label>
          )}
          <button type="button" className={styles.secondary} onClick={onLead} disabled={busy}>
            {leadSaved ? "Заявка" : <><span className={styles.long}>Оставить заявку</span><span className={styles.short}>Заявка</span></>}
          </button>
          <button type="button" className={styles.ghost} onClick={onNewDialog} disabled={booting} aria-label="Новый диалог">
            <span className={styles.long}>Новый диалог</span>
            <span className={styles.short} aria-hidden="true"><Icon name="plus" size={20} /></span>
          </button>
        </div>
      </div>
    </header>
  );
}
