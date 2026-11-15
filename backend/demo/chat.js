const ui = Object.fromEntries([...document.querySelectorAll("[id]")].map(node => [node.id, node]));
const storageKey = "energytool-demo-v1";
let session;
try {
  session = JSON.parse(sessionStorage.getItem(storageKey));
} catch {
  session = null;
}
let busy = false;
let activeTurn = null;
let lead;
let leadOperation = null;
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
  const leadBusy = Boolean(leadOperation);
  ui.new.disabled = busy;
  ui.send.disabled = busy || leadBusy || Boolean(session?.pending);
  ui.retry.hidden = busy || leadBusy || !session?.pending;
  ui.cancel.hidden = !activeTurn;
  for (const control of ui.contacts.elements) {
    control.disabled = busy || leadBusy;
  }
  ui.save.disabled = busy || leadBusy;
  ui.refuse.disabled = busy || leadBusy;
}

async function api(path, {method = "GET", body, clientToken} = {}) {
  const headers = {};
  if (clientToken) headers["X-Client-Token"] = clientToken;
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

function sessionApi(client, path = "", options) {
  return api(`/sessions/${encodeURIComponent(client.session_id)}${path}`, {
    ...options, clientToken: client.client_token,
  });
}

function renderLead(next) {
  lead = next;
  ui.lead.hidden = !lead || lead.status === "none";
  if (ui.lead.hidden) {
    ui.phone.value = "";
    ui["full-name"].value = "";
    ui.company.value = "";
    ui["lead-status"].textContent = "";
    ui["lead-preview"].textContent = "";
    ui.save.hidden = true;
    return;
  }
  ui["lead-status"].textContent = lead.status_text;
  ui.phone.value = lead.contact.phone || "";
  ui["full-name"].value = lead.contact.full_name || "";
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
  const client = session;
  const article = document.createElement("article");
  article.className = message.role;
  article.dataset.interrupted = String(!message.in_context);
  const label = document.createElement("strong");
  label.textContent = message.role === "user" ? "Вы" : "Консультант";
  const text = document.createElement("p");
  text.textContent = message.content;
  article.append(label, text);
  if (message.role === "assistant" && message.in_context && client.debug) {
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
        const trace = await sessionApi(client, `/turns/${encodeURIComponent(message.request_id)}/debug`);
        if (session !== client) return;
        const used = trace.used.map(item => item.doc_title || item.section || item.evidence_id);
        details.textContent = [used.length ? `Источники ответа: ${used.join(", ")}` : "Ответ без источников",
          ...trace.timings.map(item => `${item.name}: ${Math.round(item.ms)} мс`)].join("\n");
      } catch (error) {
        if (session === client) details.textContent = error.message;
      }
    });
    article.append(sources, details);
  }
  return article;
}

async function refresh(client = session) {
  const current = await sessionApi(client);
  if (session !== client) return;
  ui.messages.replaceChildren(...current.messages.map(renderMessage));
  renderLead(current.lead);
}

async function newSession(close = false) {
  const previous = session;
  const created = await api("/sessions", {
    method: "POST",
    body: close ? {close_session_id: previous.session_id} : {},
    clientToken: previous?.client_token,
  });
  session = {...created, debug: previous?.debug};
  leadOperation = null;
  saveKeys.clear();
  ui.messages.replaceChildren();
  renderLead(null);
  remember();
  ui.message.value = "";
  await refresh();
}

async function send() {
  const client = session;
  const pending = client.pending;
  activeTurn = pending;
  busy = true;
  controls();
  status("Готовим ответ…");
  try {
    const result = await sessionApi(client, "/messages", {method: "POST", body: pending});
    client.debug = result.debug_available;
    client.pending = null;
    remember();
    ui.message.value = "";
    try {
      await refresh(client);
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
      client.pending = null;
      remember();
    }
    try { await refresh(client); } catch { /* keep the previous history visible */ }
    status(error.status ? error.message : "Связь прервалась. История сохранена, сообщение можно повторить.", true);
  } finally {
    activeTurn = null;
    busy = false;
    controls();
  }
}

ui.chat.addEventListener("submit", async event => {
  event.preventDefault();
  const text = ui.message.value.trim();
  if (busy || leadOperation || session?.pending || !text) return;
  if (!session) {
    status("Откройте новый диалог и повторите отправку.", true);
    return;
  }
  session.pending = {request_id: requestId(), text};
  remember();
  await send();
});
ui.retry.addEventListener("click", () => { if (!busy && !leadOperation && session?.pending) send(); });
ui.cancel.addEventListener("click", async () => {
  const client = session;
  const pending = activeTurn;
  if (!pending) return;
  try {
    await sessionApi(client, `/turns/${encodeURIComponent(pending.request_id)}/cancel`, {method: "POST"});
  } catch (error) {
    if (session === client && client.pending === pending) status(error.message, true);
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
  const client = session;
  if (!client || busy || leadOperation) return;
  leadOperation = client;
  controls();
  try {
    const next = await sessionApi(client, `/lead/${path}`, options);
    if (session !== client) return;
    renderLead(next);
    status();
  } catch (error) {
    if (session !== client) return;
    if (error.code === "stale_offer") {
      try { await refresh(client); } catch { /* show the original error */ }
    }
    if (session === client) status(error.message, true);
  } finally {
    if (leadOperation === client) {
      leadOperation = null;
      controls();
    }
  }
}

ui.contacts.addEventListener("submit", event => {
  event.preventDefault();
  return updateLead("contacts", {method: "PUT", body: {
    phone: ui.phone.value,
    full_name: ui["full-name"].value || null,
    company: ui.company.value || null,
  }});
});
ui.contacts.addEventListener("input", () => { ui.save.hidden = true; });
ui.save.addEventListener("click", () => {
  if (ui.save.disabled || ui.save.hidden || !lead?.offer_id) return;
  const offerId = lead.offer_id;
  if (!saveKeys.has(offerId)) saveKeys.set(offerId, requestId());
  return updateLead("save", {method: "POST", body: {offer_id: offerId, idempotency_key: saveKeys.get(offerId)}});
});
ui.refuse.addEventListener("click", () => updateLead("refuse", {method: "POST"}));

async function start() {
  busy = true;
  controls();
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
  } finally {
    busy = false;
    controls();
  }
}

start();
