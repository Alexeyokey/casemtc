import type { ConsultationApi } from "./api/adapter";
import type { AppConfig } from "./config";
import fixtures from "./fixtures/consultation.json";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { LeadPanel } from "./components/LeadPanel";
import { MessageList } from "./components/MessageList";
import { TopBar } from "./components/TopBar";
import { visibleMessages } from "./state/chatState";
import { useConsultation } from "./state/useConsultation";
import styles from "./App.module.css";

export function App({ api, config }: { api: ConsultationApi; config: AppConfig }) {
  const { state, actions } = useConsultation(api);
  const messages = visibleMessages(state);
  const booting = state.phase === "booting";
  const isMock = config.apiMode === "mock";
  const isStub = Boolean(state.engine?.includes("stub_engine"));

  if (state.phase === "fatal") {
    return (
      <div className={styles.fatal} role="alert">
        <h1>Чат сейчас недоступен</h1>
        <p>{state.notice?.text}</p>
        <button type="button" onClick={() => window.location.reload()}>
          Обновить страницу
        </button>
      </div>
    );
  }

  return (
    <div className={styles.app}>
      <a className={styles.skip} href="#composer">
        К полю сообщения
      </a>
      <TopBar
        showDebugToggle={config.debugToggle}
        debugOn={state.debugOn}
        onToggleDebug={actions.toggleDebug}
        onLead={actions.openLead}
        onNewDialog={actions.newDialog}
        busy={booting || state.sending}
        booting={booting}
        leadSaved={state.lead?.status === "saved"}
      />
      <main className={styles.main} aria-busy={booting}>
        {(isMock || isStub) && (
          <p className={styles.mode}>
            {isMock
              ? "Демо-режим: ответы из синтетических фикстур, сервер не подключён."
              : "Демо-консультант: база знаний и модель организаторов не подключены."}
          </p>
        )}
        {booting ? (
          <div className={styles.skeleton} aria-label="Загружаем чат">
            <span />
            <span />
            <span />
          </div>
        ) : messages.length === 0 && !state.leadOpen ? (
          <EmptyState suggestions={fixtures.suggestions} onPick={actions.send} disabled={state.sending} />
        ) : (
          <MessageList
            messages={messages}
            waiting={state.sending}
            debugOn={state.debugOn}
            loadTrace={actions.loadTrace}
          >
            {state.leadOpen && (
              <LeadPanel
                key={state.session?.session_id}
                lead={state.lead}
                busy={state.leadOp}
                chatBusy={state.sending}
                fieldErrors={state.fieldErrors}
                formError={state.formError}
                onSubmit={actions.submitContacts}
                onSave={actions.saveLead}
                onRefuse={actions.refuse}
                onFieldEdited={actions.fieldEdited}
              />
            )}
          </MessageList>
        )}
      </main>
      <Composer
        draft={state.draft}
        onDraft={actions.setDraft}
        onSend={actions.send}
        onRetry={actions.retry}
        onDismiss={actions.dismissNotice}
        sending={state.sending}
        blocked={booting || Boolean(state.leadOp)}
        hasPending={Boolean(state.pending) && !state.sending}
        notice={state.notice}
      />
    </div>
  );
}
