import assert from "node:assert/strict";
import {webcrypto} from "node:crypto";
import {readFileSync} from "node:fs";
import test from "node:test";
import {setImmediate} from "node:timers/promises";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../backend/demo/chat.js", import.meta.url), "utf8");
const page = readFileSync(new URL("../backend/demo/index.html", import.meta.url), "utf8");

class Element {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.dataset = {};
    this.listeners = new Map();
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  fire(type) {
    return this.listeners.get(type)?.({preventDefault() {}});
  }

  replaceChildren() {}

  focus() {}
}

const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});

function pendingResponse() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function readyLead(company = "Тестовая компания") {
  return {
    status: "ready",
    status_text: "Проверьте контакты",
    contact: {company, phone: "+79990000031"},
    offer_id: "test-offer",
  };
}

async function openChat({handle, nextLead = null}) {
  const elements = Object.fromEntries([...page.matchAll(/\bid="([^"]+)"/g)]
    .map(([, id]) => [id, new Element(id)]));
  const {contacts, phone, company} = elements;
  contacts.elements = [phone, company, new Element("contacts-submit")];
  const requests = [];
  runInNewContext(script, {
    document: {querySelectorAll: () => Object.values(elements)},
    sessionStorage: {
      getItem: () => JSON.stringify({session_id: "old", client_token: "old-token"}),
      setItem() {},
    },
    crypto: webcrypto,
    fetch(path, options) {
      const request = {path, token: options.headers["X-Client-Token"], method: options.method,
        body: options.body && JSON.parse(options.body)};
      requests.push(request);
      const override = handle(request);
      if (override) return override;
      if (path === "/api/v1/health") return response({engine: "stub_engine"});
      if (path === "/api/v1/sessions/old") return response({messages: [], lead: readyLead()});
      if (path === "/api/v1/sessions/new") return response({messages: [], lead: nextLead});
      if (path === "/api/v1/sessions" && request.method === "POST") {
        assert.equal(request.token, "old-token");
        assert.equal(request.body.close_session_id, "old");
        return response({session_id: "new", client_token: "new-token"}, 201);
      }
      assert.fail(`Unexpected request: ${request.method} ${path}`);
    },
  });
  await setImmediate();
  assert.equal(elements.lead.hidden, false);
  return {elements, requests};
}

test("a contacts response from the previous dialog is ignored after starting a new dialog", async () => {
  const delayed = pendingResponse();
  const {elements, requests} = await openChat({handle: request => (
    request.path.endsWith("/lead/contacts") ? delayed.promise : null
  )});

  elements.contacts.fire("submit");
  assert.equal(requests.at(-1).token, "old-token");
  assert.equal(elements.send.disabled, true);
  assert.equal(elements.new.disabled, false);
  await elements.new.fire("click");
  assert.equal(requests.at(-1).token, "new-token");
  delayed.resolve(response(readyLead("Старая компания")));
  await setImmediate();

  assert.equal(elements.lead.hidden, true);
  assert.equal(elements.phone.value, "");
  assert.equal(elements.company.value, "");
  assert.equal(elements.save.hidden, true);
  assert.equal(elements.send.disabled, false);
  assert.equal(elements.status.textContent, "");
});

test("retry reuses the lead key within one dialog and a new dialog creates its own key", async () => {
  const delayed = pendingResponse();
  let count = 0;
  const {elements, requests} = await openChat({nextLead: readyLead(), handle: request => {
    if (!request.path.endsWith("/lead/save")) return null;
    if (++count === 1) return delayed.promise;
    return response({...readyLead(), status: "saved"});
  }});

  const firstSave = elements.save.fire("click");
  await elements.save.fire("click");
  assert.equal(count, 1);
  delayed.resolve(response({error: {message: "Повторите сохранение", retryable: true}}, 503));
  await firstSave;
  assert.equal(elements.save.disabled, false);
  await elements.save.fire("click");
  await elements.new.fire("click");
  await elements.save.fire("click");

  const saves = requests.filter(request => request.path.endsWith("/lead/save"));
  assert.equal(saves.length, 3);
  assert.equal(saves[0].body.idempotency_key, saves[1].body.idempotency_key);
  assert.notEqual(saves[1].body.idempotency_key, saves[2].body.idempotency_key);
  assert.deepEqual(saves.map(request => request.token), ["old-token", "old-token", "new-token"]);
});

test("a stale offer error cannot replace the new dialog status or unlock its save", async () => {
  const oldSave = pendingResponse();
  const newSave = pendingResponse();
  const {elements, requests} = await openChat({nextLead: readyLead(), handle: request => {
    if (request.path.endsWith("/old/lead/save")) return oldSave.promise;
    if (request.path.endsWith("/new/lead/save")) return newSave.promise;
    return null;
  }});

  const oldResult = elements.save.fire("click");
  await elements.new.fire("click");
  const newResult = elements.save.fire("click");
  oldSave.resolve(response({error: {code: "stale_offer", message: "Старая ошибка"}}, 409));
  await oldResult;

  assert.equal(elements.status.textContent, "");
  assert.equal(elements.save.disabled, true);
  assert.equal(requests.filter(request => request.path.endsWith("/sessions/old")).length, 1);

  newSave.resolve(response({...readyLead(), status: "saved", status_text: "Заявка сохранена"}));
  await newResult;
  assert.equal(elements["lead-status"].textContent, "Заявка сохранена");
});
