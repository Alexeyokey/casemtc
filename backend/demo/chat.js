const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(node => [node.id, node]));
const storageKey = "energytool-demo-v1";
let session;
try {
  session = JSON.parse(sessionStorage.getItem(storageKey));
} catch {
  session = null;
}
let busy = false;
let lead;
const saveKeys = new Map();

function remember() {
  try {
    sessionStorage.setItem(storageKey, JSON.stringify(session));
  } catch {
    // The open tab still works when storage is unavailable.
  }
}

function requestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
}

function status(text = "", error = false) {
  ui.status.textContent = text;
  ui.status.dataset.error = String(error);
}

function controls() {
  ui.new.disabled = busy;
  ui.send.disabled = busy || Boolean(session?.pending);
  ui.retry.hidden = busy || !session?.pending;
  ui.cancel.hidden = !busy || !session?.pending;
}

async function api(path, {method = "GET", body} = {}) {
  const headers = {};
  if (session?.client_token) headers["X-Client-Token"] = session.client_token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/v1${path}`, {method, headers, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    const problem = new Error(data.error?.message || "Запрос не выполнен.");
    problem.code = data.error?.code;
    problem.retryable = data.error?.retryable;
    problem.status = response.status;
    throw problem;
  }
  return data;
}

function sessionPath(path = "") {
  return `/sessions/${encodeURIComponent(session.session_id)}${path}`;
}

function renderLead(next) {
  lead = next;
  ui.lead.hidden = !lead || lead.status === "none";
  if (ui.lead.hidden) return;
  ui["lead-status"].textContent = lead.status_text;
  ui.phone.value = lead.contact.phone || "";
  ui.company.value = lead.contact.company || "";
  const summary = lead.summary;
  ui["lead-preview"].textContent = summary ? [summary.task, ...summary.known_facts,
    ...summary.assumptions.map(value => `Уточнить: ${value}`),
    ...summary.discussed_solutions, ...summary.unknowns.map(value => `Не указано: ${value}`),
    summary.next_step].join("\n") : "";
  ui.contacts.hidden = lead.contact_refused;
  ui.save.hidden = lead.status !== "ready" || !lead.offer_id;
  ui.refuse.hidden = lead.contact_refused || lead.status === "saved";
}

function renderMessage(message) {
  const article = document.createElement("article");
  article.className = message.role;
  article.dataset.interrupted = String(!message.in_context);
  const label = document.createElement("strong");
  label.textContent = message.role === "user" ? "Вы" : "Консультант";
  const text = document.createElement("p");
  text.textContent = message.content;
  article.append(label, text);
  if (message.role === "assistant" && message.in_context && session.debug) {
    const sources = document.createElement("button");
    sources.textContent = "Источники и время";
    const details = document.createElement("p");
    details.hidden = true;
    sources.addEventListener("click", async () => {
      if (!details.hidden) {
        details.hidden = true;
        return;
      }
      details.hidden = false;
      details.textContent = "Загрузка…";
      try {
        const trace = await api(sessionPath(`/turns/${encodeURIComponent(message.request_id)}/debug`));
        const used = trace.used.map(item => item.doc_title || item.section || item.evidence_id);
        details.textContent = [used.length ? `Источники ответа: ${used.join(", ")}` : "Ответ без источников",
          ...trace.timings.map(item => `${item.name}: ${Math.round(item.ms)} мс`)].join("\n");
      } catch (error) {
        details.textContent = error.message;
      }
    });
    article.append(sources, details);
  }
  return article;
}

async function refresh() {
  const current = await api(sessionPath());
  ui.messages.replaceChildren(...current.messages.map(renderMessage));
  renderLead(current.lead);
}

async function newSession(close = false) {
  const created = await api("/sessions", {method: "POST", body: close ? {close_session_id: session.session_id} : {}});
  session = {...created, debug: session?.debug};
  remember();
  ui.message.value = "";
  await refresh();
}

async function send() {
  busy = true;
  controls();
  status("Готовим ответ…");
  const pending = session.pending;
  try {
    const result = await api(sessionPath("/messages"), {method: "POST", body: pending});
    session.debug = result.debug_available;
    session.pending = null;
    remember();
    ui.message.value = "";
    try {
      await refresh();
      status();
    } catch {
      const messages = [
        {role: "user", content: pending.text},
        {role: "assistant", content: result.answer},
      ];
      ui.messages.append(...messages.map(message => renderMessage({
        ...message, request_id: pending.request_id, in_context: true,
      })));
      renderLead(result.lead);
      status("Ответ получен. Не удалось обновить историю целиком.", true);
    }
  } catch (error) {
    if (error.code === "turn_cancelled" || error.status && !error.retryable) {
      session.pending = null;
      remember();
    }
    try { await refresh(); } catch { /* keep the previous history visible */ }
    status(error.status ? error.message : "Связь прервалась. История сохранена, сообщение можно повторить.", true);
  } finally {
    busy = false;
    controls();
  }
}

ui.chat.addEventListener("submit", async event => {
  event.preventDefault();
  const text = ui.message.value.trim();
  if (busy || session?.pending || !text) return;
  if (!session) {
    status("Откройте новый диалог и повторите отправку.", true);
    return;
  }
  session.pending = {request_id: requestId(), text};
  remember();
  await send();
});
ui.retry.addEventListener("click", () => { if (!busy && session?.pending) send(); });
ui.cancel.addEventListener("click", async () => {
  if (!session?.pending) return;
  try {
    await api(sessionPath(`/turns/${session.pending.request_id}/cancel`), {method: "POST"});
  } catch (error) {
    status(error.message, true);
  }
});
ui.new.addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  controls();
  try {
    await newSession(Boolean(session));
    status();
    ui.message.focus();
  } catch (error) {
    status(error.message, true);
  } finally {
    busy = false;
    controls();
  }
});

async function updateLead(path, options) {
  try {
    renderLead(await api(sessionPath(`/lead/${path}`), options));
    status();
  } catch (error) {
    if (error.code === "stale_offer") {
      try { await refresh(); } catch { /* show the original error */ }
    }
    status(error.message, true);
  }
}

ui.contacts.addEventListener("submit", event => {
  event.preventDefault();
  updateLead("contacts", {method: "PUT", body: {phone: ui.phone.value, company: ui.company.value || null}});
});
ui.contacts.addEventListener("input", () => { ui.save.hidden = true; });
ui.save.addEventListener("click", async () => {
  if (!lead?.offer_id) return;
  const offerId = lead.offer_id;
  if (!saveKeys.has(offerId)) saveKeys.set(offerId, requestId());
  ui.save.disabled = true;
  try {
    await updateLead("save", {method: "POST", body: {offer_id: offerId, idempotency_key: saveKeys.get(offerId)}});
  } finally {
    ui.save.disabled = false;
  }
});
ui.refuse.addEventListener("click", () => updateLead("refuse", {method: "POST"}));

async function start() {
  try {
    const health = await api("/health");
    if (!health.engine.includes("stub_engine")) ui.mode.textContent = "Консультант подключён к базе знаний.";
    if (session?.session_id) {
      try { await refresh(); } catch (error) {
        if (error.status !== 404) throw error;
        session = null;
        await newSession();
      }
      if (session.pending) {
        ui.message.value = session.pending.text;
        status("Можно повторить отправку последнего сообщения.");
      }
    } else {
      await newSession();
    }
  } catch (error) {
    status(`Не удалось открыть чат. ${error.message}`, true);
  }
  controls();
}

start();
