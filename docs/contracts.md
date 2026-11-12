# Контракты ядра и API (S1, #2)

Версия контрактов: **1.0.0** (`core.contracts.CONTRACT_VERSION`). Источник истины — Python DTO в `core/contracts.py` и `backend/api_models.py`. JSON Schema и синтетические примеры генерируются из них:

```bash
python scripts/gen_contracts.py          # обновить contracts/
python scripts/gen_contracts.py --check  # проверка в CI (входит в pytest)
```

| Где | Что |
|---|---|
| `contracts/schemas/*.schema.json` | JSON Schema всех DTO |
| `contracts/fixtures/core/*.json` | Примеры входа/выхода ядра |
| `contracts/fixtures/http/*.json` | Примеры HTTP-запросов/ответов для web |

Все контакты и тексты в fixtures вымышленные. Фрагментов закрытой БЗ нет.

## 1. Ядро: `ConsultationEngine`

```python
class ConsultationEngine(Protocol):
    def consult(self, request: ConsultationRequest) -> ConsultationResult: ...
```

- Контракт ядра не зависит от HTTP, CSV и БД. В режиме с базой веб-адаптер `RagEngine` и платформенный `solution.py` используют общий `RagConsultant` и клиент организаторов. Веб-адаптер дополняет результат состоянием контактов и отладкой.
- `CONSULTATION_ENGINE=module:Factory` задаёт веб-реализацию явно. При `ENERGYTOOL_KB_PATH` веб подключает `RagEngine`. Без настроек запускается синтетический `StubEngine`.
- Консультант получает состояние в запросе. Архив остаётся в хранилище веб-сервера.

### `ConsultationRequest`

| Поле | Смысл |
|---|---|
| `session_id`, `request_id` | Идентификатор диалога и реплики |
| `history: Message[]` | Завершённые реплики **только текущей сессии**, по порядку. Ходы с ошибкой или отменой сюда не попадают |
| `message` | Текущая реплика клиента |
| `client_context: ClientContext` | Состояние консультации, возвращённое предыдущим ходом той же сессии. Для новой сессии пустое |
| `debug` | Заполнить `retrieved` в результате |

Платформенный CSV обрабатывает каждую строку как отдельную консультацию. `solution.py` читает историю из `question` и передаёт её в `RagConsultant`.

### `ClientContext` (семантика полей — C1, Максим)

`topic`, `facts` (сказано клиентом), `assumptions` (предположения консультанта, не факты), `contact: ContactInfo`, `contact_status` (`unknown | requested | partial | phone_valid | refused`), `callback_requested`, `extra` (расширения без смены версии).

### `ConsultationResult`

| Поле | Смысл |
|---|---|
| `answer` | Текст клиенту. Без имён файлов, rules и сырого RAG-контекста |
| `action` | `answer`, `clarify`, `collect_contacts`, `lead_ready`, `escalate`, `out_of_scope` |
| `clarifying_question` | Обязателен при `action=clarify` |
| `used_evidence_ids` | Фрагменты, на которые **реально** опирается ответ. Подмножество `retrieved` (проверяется валидатором) |
| `retrieved: Evidence[]` | Всё, что вернул поиск (R3). Попадает только в debug/trace |
| `client_context` | Обновлённое состояние для следующего хода |
| `lead_draft` | Контакты и `LeadSummary`, если клиент просит связь или дал контакт |
| `timings: StageTiming[]` | Реально измеренные этапы (`retrieval`, `generation`, ...) |

### `Evidence` (R3, Алексей)

`evidence_id` (стабильный внутри индекса), `doc_id`, `doc_title`, `section`, `text`, `score`, `retriever`.

### `LeadSummary`

`task`, `known_facts`, `assumptions`, `discussed_solutions`, `unknowns`, `next_step`. Известное, предполагаемое и неизвестное разделены. Компанию, бюджет и обещания, которых клиент не называл и которых нет в БЗ, не пишем.

## 2. Контакты и заявка

- Телефон нормализуется в E.164 (`core/phone.py`): РФ `+7`/`8`/10 цифр, международный формат с `+`.
- **Черновик** (`draft`) — нет валидного телефона. Консультация и история продолжаются.
- **Готова** (`ready`) — телефон прошёл проверку формата. ФИО, компания и регион предлагаются в `missing_fields`. Предпросмотр содержит `offer_id`, который связывает подтверждение с показанными контактами и резюме.
- **Сохранена** (`saved`) — подтверждённая ревизия в SQLite. Из неё строится Markdown. Редактирование контактов и продолжение диалога меняют черновик. Для изменения сохранённой ревизии требуется новое подтверждение. `delivery=stored_only` означает сохранение в системе (QA06).
- **Отказ** — `contact_refused=true`. Текущий черновик теряет возможность сохранения. Консультация продолжается.

## 3. HTTP API (`/api/v1`)

Заголовок `X-Client-Token` (выдаётся при создании сессии) нужен для всех операций с сессией. Чужая или несуществующая сессия → `404 session_not_found`.

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/sessions` | Новый диалог. Body `{close_session_id?}` закрывает прежний (кнопка «Новый диалог»). → `SessionCreated` |
| GET | `/sessions/{id}` | История и состояние заявки → `SessionView` |
| POST | `/sessions/{id}/close` | Закрыть диалог (архив сохраняется) |
| POST | `/sessions/{id}/messages` | Ход: `TurnRequest{request_id,text}` → `TurnResponse` |
| POST | `/sessions/{id}/messages/stream` | То же через SSE: `event: accepted` → `final` \| `error` |
| POST | `/sessions/{id}/turns/{request_id}/cancel` | Отмена хода |
| GET | `/sessions/{id}/turns/{request_id}/debug` | Трасса выбранного ответа → `TraceView` |
| GET | `/sessions/{id}/lead` | Предпросмотр заявки → `LeadView` |
| PUT | `/sessions/{id}/lead/contacts` | Частичное обновление контактов (`ContactsUpdate`) |
| POST | `/sessions/{id}/lead/refuse` | Клиент отказался от контактов |
| POST | `/sessions/{id}/lead/save` | Подтвердить показанный черновик `{offer_id,idempotency_key}` → `LeadView` |
| GET | `/sessions/{id}/lead.md` | Человекочитаемая заявка |
| GET | `/archive/sessions`, `/archive/sessions/{id}`, `/archive/export.jsonl`, `/archive/leads`, `/archive/leads/{id}.md` | Архив для проверяющего (`X-Archive-Token`) |
| GET | `/health` | Готовность SQLite, версия контракта, подключённый engine и SHA сборки при наличии |

### Ход, повтор, отмена

- `request_id` генерирует клиент (UUID) на каждую **новую** реплику. Повтор отправки идёт с **тем же** `request_id`.
- Повтор завершённого хода → тот же ответ, `replayed=true`, без дубля сообщений.
- Тот же `request_id` с другим текстом → `409 request_id_conflict`.
- Пока идёт ход → `409 turn_in_progress` (`retryable=true`).
- SSE отправляет `accepted` после сохранения хода в БД. С этого момента его можно отменить. Ошибка до регистрации хода приходит отдельным `error`.
- Ошибка консультанта → `502 engine_error`. Ошибка хранилища → `500 storage_error`. Сообщение остаётся в архиве. Повтор с тем же ID доступен, пока следующий ход ещё не принят. Если он уже принят, сервер возвращает `409 turn_superseded`. Старую реплику можно отправить новым сообщением.
- Если отмена записана первой, клиент получает `409 turn_cancelled`. Отменённые реплики исключаются из контекста консультанта. Если ответ уже сохранён, отмена возвращает `completed`.
- Закрытая сессия → `409 session_closed`.

### Подтверждение заявки

Отправляйте `offer_id` из показанного пользователю предпросмотра. При `409 stale_offer` загрузите новый предпросмотр и попросите подтвердить его. При обрыве связи или `503 lead_file_unavailable` повторите исходный запрос с теми же `offer_id` и `idempotency_key`. Ревизия заявки при таком повторе сохраняется.

### Ошибки

```json
{"error": {"code": "turn_in_progress", "message": "текст для пользователя", "request_id": "…", "retryable": true}}
```

Коды чата: `session_not_found`, `session_closed`, `turn_in_progress`, `request_id_conflict`, `turn_cancelled`, `turn_superseded`, `turn_not_found`, `engine_error`, `storage_error`. Коды заявки: `invalid_phone`, `phone_required`, `contacts_refused`, `stale_offer`, `lead_request_conflict`, `lead_file_unavailable`, `lead_not_found`. Служебные коды: `trace_not_found`, `debug_disabled`, `archive_forbidden`, `validation_error`, `internal_error`.

## 4. Разделение ответа, debug и архива

| Слой | Кому | Что содержит |
|---|---|---|
| `TurnResponse` | Клиент в чате | Текст, действие, состояние заявки. Без источников |
| `TraceView` | Переключаемый debug (U4) | Метаданные найденных и использованных источников, время и тип ошибки. Текст фрагментов доступен при одновременной передаче `X-Client-Token` и `X-Archive-Token`. Секреты и телефоны замаскированы |
| Архив | Проверяющий/менеджер | Полные сессии, ходы, заявки: `python -m backend.archive` или `/archive/*` |

Архив никогда не подмешивается в контекст новой сессии.

## 5. Правила изменения контрактов

1. Изменения — отдельным PR с id задачи. Владельцы затронутых блоков (C1/R3/U2–U4/I2) указываются ревьюерами.
2. Добавление необязательного поля → PATCH/MINOR (`1.0.x`/`1.x.0`). Удаление, переименование или смена смысла поля → MAJOR.
3. После правки DTO запустить `python scripts/gen_contracts.py` и закоммитить `contracts/`. `pytest` упадёт, если схемы устарели.
4. Расширения C1 без смены версии — через `ClientContext.extra`.
