import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError, type ConsultationApi } from "./api/adapter";
import { createHttpApi } from "./api/httpAdapter";
import { createMockApi } from "./api/mockAdapter";
import { App } from "./App";
import { loadConfig } from "./config";

const config = { apiBaseUrl: "", apiMode: "mock" as const, debugToggle: true };

beforeEach(() => {
  sessionStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
  window.requestAnimationFrame = (cb: FrameRequestCallback) => {
    cb(0);
    return 0;
  };
});

async function setup(api: ConsultationApi = createMockApi({ latencyMs: 0 })) {
  const user = userEvent.setup();
  render(<App api={api} config={config} />);
  await screen.findByRole("heading", { name: /Что нужно измерять/ });
  return { user, api };
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  const box = screen.getByLabelText("Сообщение консультанту");
  await user.clear(box);
  await user.type(box, text);
  await user.keyboard("{Enter}");
}

describe("Веб-чат", () => {
  it("ведёт от вопроса до сохранённой заявки только с телефоном", async () => {
    const { user } = await setup();
    await ask(user, "Как вести учёт электроэнергии на 3 складах?");
    expect(await screen.findByText(/продукт А/)).toBeInTheDocument();

    await ask(user, "Свяжите меня со специалистом");
    const phone = await screen.findByLabelText(/Телефон/);
    await user.type(phone, "8 999 000 00 01");
    await user.click(screen.getByRole("button", { name: "Проверить данные" }));

    const save = await screen.findByRole("button", { name: "Сохранить заявку" });
    expect(screen.getByLabelText("Что увидит специалист")).toHaveTextContent("Не указано: ФИО, Компания");
    await user.dblClick(save);

    const receipt = await screen.findByRole("heading", { name: "Заявка сохранена" });
    const card = receipt.closest("section")!;
    expect(card).toHaveTextContent("+7 999 000-00-01");
    expect(card).toHaveTextContent("Отправка в CRM или на почту не подключена");
    expect(card).not.toHaveTextContent(/менеджер (уведомлён|свяжется)|звонок назначен/i);
    expect(within(card).getByText(/^1,/)).toBeInTheDocument(); // версия 1 — двойной клик не создал вторую
  });

  it("показывает ошибку телефона рядом с полем и не теряет введённое", async () => {
    const { user } = await setup();
    await user.click(screen.getByRole("button", { name: /Оставить заявку/ }));
    const phone = await screen.findByLabelText(/Телефон/);
    await user.type(phone, "12-34");
    await user.click(screen.getByRole("button", { name: "Проверить данные" }));
    expect(phone).toHaveAttribute("aria-invalid", "true");
    const describedBy = phone.getAttribute("aria-describedby")!;
    expect(document.getElementById(describedBy.split(" ")[0])).toHaveTextContent("Номер не распознан");
    expect(phone).toHaveValue("12-34");
  });

  it("сохраняет переписку при сбое и повторяет тем же request_id без дубля", async () => {
    const api = createMockApi({ latencyMs: 0 });
    const spy = vi.spyOn(api, "sendMessage");
    const { user } = await setup(api);
    await ask(user, "#сбой учёт электроэнергии");
    const retry = await screen.findByRole("button", { name: "Повторить" });
    expect(screen.getByRole("alert")).toHaveTextContent("Не удалось подготовить ответ");
    expect(screen.getByLabelText("Сообщение консультанту")).toHaveValue("#сбой учёт электроэнергии");
    await user.click(retry);
    await screen.findByText(/продукт А/);
    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy.mock.calls[0][1].request_id).toBe(spy.mock.calls[1][1].request_id);
    expect(screen.getAllByText("#сбой учёт электроэнергии")).toHaveLength(1);
  });

  it("блокирует повторную отправку, пока ответ в пути", async () => {
    let release!: () => void;
    const base = createMockApi({ latencyMs: 0 });
    const gate = new Promise<void>((r) => (release = r));
    const api: ConsultationApi = { ...base, sendMessage: async (s, t) => (await gate, base.sendMessage(s, t)) };
    const spy = vi.spyOn(api, "sendMessage");
    const { user } = await setup(api);
    await user.type(screen.getByLabelText("Сообщение консультанту"), "учёт электроэнергии");
    await user.keyboard("{Enter}");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /Отправляем/ })).toBeDisabled();
    await act(async () => release());
    await screen.findByText(/продукт А/);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("не показывает ответ старого диалога после «Новый диалог»", async () => {
    let release!: () => void;
    const base = createMockApi({ latencyMs: 0 });
    const gate = new Promise<void>((r) => (release = r));
    const api: ConsultationApi = {
      ...base,
      sendMessage: async (s, t) => {
        const res = await base.sendMessage(s, t);
        await gate;
        return res;
      },
    };
    const { user } = await setup(api);
    await ask(user, "учёт электроэнергии на складе");
    await user.click(screen.getByRole("button", { name: "Новый диалог" }));
    await screen.findByRole("heading", { name: /Что нужно измерять/ });
    await act(async () => release());
    await waitFor(() => expect(screen.queryByText(/продукт А/)).not.toBeInTheDocument());
  });

  it("в отладке показывает документы ответа и не выводит тексты фрагментов", async () => {
    const { user } = await setup();
    await ask(user, "учёт электроэнергии");
    await screen.findByText(/продукт А/);
    await user.click(screen.getByRole("switch"));
    await user.click(screen.getByRole("button", { name: "Источники и статус" }));
    const panel = await screen.findByText("Использовано");
    const grid = panel.closest("dl")!;
    expect(grid).toHaveTextContent("Вымышленный продукт А");
    expect(grid).toHaveTextContent("Найдено, но не использовано");
    expect(grid).not.toHaveTextContent(/client_token|X-Client-Token|evidence_id|syn-001/);
  });
});

describe("Адаптеры и конфиг", () => {
  it("HTTP-адаптер превращает ответ сервера в ApiError с полем", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ error: { code: "invalid_phone", message: "Номер не распознан", request_id: null, retryable: false } }), { status: 422 }),
    );
    const api = createHttpApi("https://example.test", fetchImpl as unknown as typeof fetch);
    const err = await api.updateContacts({ session_id: "s", client_token: "t" }, { phone: "1" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.field).toBe("phone");
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://example.test/api/v1/sessions/s/lead/contacts");
    expect((init.headers as Record<string, string>)["X-Client-Token"]).toBe("t");
  });

  it("сетевой сбой — повторяемая ошибка", async () => {
    const api = createHttpApi("", (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch);
    const err = await api.health().catch((e) => e);
    expect(err.isNetwork).toBe(true);
    expect(err.retryable).toBe(true);
  });

  it("конфиг читает режим и отладку из env и URL, без секретов", () => {
    expect(loadConfig({ VITE_API_MODE: "mock", VITE_API_BASE_URL: "https://x.test/" }, "")).toEqual({
      apiBaseUrl: "https://x.test", apiMode: "mock", debugToggle: false,
    });
    expect(loadConfig({}, "?debug=1").debugToggle).toBe(true);
  });
});
