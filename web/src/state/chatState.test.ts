import { initialState, reducer, visibleMessages, type ChatState } from "./chatState";

const session = { session_id: "s_a", client_token: "t" };

function started(): ChatState {
  return reducer(initialState, { type: "session_started", session });
}

describe("chatState reducer", () => {
  it("ignores a reply that arrives after the user started a new dialog", () => {
    let s = started();
    const oldEpoch = s.epoch;
    s = reducer(s, { type: "send_started", pending: { request_id: "r1", text: "вопрос" } });
    s = reducer(s, { type: "session_started", session: { session_id: "s_b", client_token: "t2" } });
    const after = reducer(s, {
      type: "send_succeeded", epoch: oldEpoch, requestId: "r1", debugAvailable: true, lead: null,
      messages: [{ id: 1, seq: 1, role: "assistant", content: "старый ответ", request_id: "r1", in_context: true, created_at: "" }],
    });
    expect(after).toBe(s);
    expect(after.messages).toHaveLength(0);
  });

  it("ignores a reply for a request that is no longer pending", () => {
    let s = started();
    s = reducer(s, { type: "send_started", pending: { request_id: "r2", text: "новый" } });
    const after = reducer(s, { type: "send_succeeded", epoch: s.epoch, requestId: "r1", messages: [], lead: null, debugAvailable: false });
    expect(after).toBe(s);
  });

  it("keeps the pending turn and the text after a retryable error", () => {
    let s = started();
    s = reducer(s, { type: "draft_changed", text: "вопрос" });
    s = reducer(s, { type: "send_started", pending: { request_id: "r1", text: "вопрос" } });
    s = reducer(s, { type: "send_failed", epoch: s.epoch, requestId: "r1", dropPending: false, notice: { tone: "error", text: "сеть", canRetry: true } });
    expect(s.pending?.request_id).toBe("r1");
    expect(s.draft).toBe("вопрос");
    expect(s.sending).toBe(false);
    expect(s.notice?.canRetry).toBe(true);
  });

  it("drops the pending turn on a non-retryable error but keeps the draft for editing", () => {
    let s = started();
    s = reducer(s, { type: "draft_changed", text: "текст" });
    s = reducer(s, { type: "send_started", pending: { request_id: "r1", text: "текст" } });
    s = reducer(s, { type: "send_failed", epoch: s.epoch, requestId: "r1", dropPending: true, notice: { tone: "error", text: "нельзя" } });
    expect(s.pending).toBeNull();
    expect(s.draft).toBe("текст");
  });

  it("shows the user's message immediately while the reply is on its way", () => {
    let s = started();
    s = reducer(s, { type: "send_started", pending: { request_id: "r1", text: "привет" } });
    expect(visibleMessages(s).map((m) => m.content)).toEqual(["привет"]);
  });

  it("puts a field error next to the field and clears it on edit", () => {
    let s = started();
    s = reducer(s, { type: "lead_op_started", op: "contacts" });
    s = reducer(s, { type: "lead_op_failed", epoch: s.epoch, field: "phone", text: "Номер не распознан" });
    expect(s.fieldErrors.phone).toBe("Номер не распознан");
    expect(s.formError).toBeNull();
    s = reducer(s, { type: "field_edited", field: "phone" });
    expect(s.fieldErrors.phone).toBeUndefined();
  });
});
