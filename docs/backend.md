# Backend: запуск, хранение, архив, debug (S2–S4)

## Запуск

```bash
pip install -r requirements-backend.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000      # один воркер
pytest -q                                                 # тесты
```

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `CONSULTATION_ENGINE` | `core.stub_engine:StubEngine` | Реализация ядра (`module:Factory`) |
| `CASEMTC_DB_PATH` | `data/casemtc.sqlite3` | SQLite с сессиями, ходами, трассами, заявками |
| `CASEMTC_LEADS_DIR` | `data/leads` | Человекочитаемые файлы заявок `<lead_id>.md` |
| `CASEMTC_DEBUG_API` | `1` | Сбор `retrieved` и debug-эндпоинт |
| `ARCHIVE_TOKEN` | — | Включает `/api/v1/archive/*`; без него архив читается только через CLI |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Origins dev-фронтенда |

`data/` и `exports/` не коммитятся: там персональные данные из диалогов.

## Хранение (SQLite, WAL)

Таблицы `sessions`, `messages`, `turns`, `traces`, `leads`. Миграции версионируются в `schema_version` и применяются при старте.

- `messages.in_context` — сообщение входит в историю для engine только после успешного хода. Сообщения ходов с ошибкой или отменой остаются в архиве с `in_context=0`.
- `turns(session_id, request_id)` — уникальный ключ идемпотентности. Статусы: `pending → completed | failed | cancelled`.
- При старте все `pending` помечаются `failed/interrupted` (процесс упал посреди хода). Их можно повторить тем же `request_id`.
- `leads.session_id` уникален: одна заявка на диалог. Повторное сохранение после правки контактов увеличивает `revision`.

**Ограничение:** блокировка «один ход на сессию» работает внутри процесса. Запускать одним воркером uvicorn, иначе нужна внешняя блокировка (Postgres/Redis). Для демо и платформы этого достаточно.

## Архив для проверяющего

```bash
python -m backend.archive --db data/casemtc.sqlite3 --out exports/          # без трасс
python -m backend.archive --db data/casemtc.sqlite3 --out exports/ --traces # с трассами
```

Результат: `exports/sessions.jsonl` (по строке на сессию: сообщения, ходы, контекст, заявка) и `exports/sessions/<id>.md` (читаемый диалог). Сессии без заявки сохраняются так же, как с заявкой.

HTTP (при заданном `ARCHIVE_TOKEN`, заголовок `X-Archive-Token`): `/api/v1/archive/sessions`, `/api/v1/archive/sessions/{id}?traces=true`, `/api/v1/archive/export.jsonl`, `/api/v1/archive/leads`, `/api/v1/archive/leads/{id}.md`.

## Debug и трассы (S4)

`GET /api/v1/sessions/{id}/turns/{request_id}/debug` возвращает трассу именно этого ответа (последняя попытка):

- `retrieved` — всё, что нашёл поиск; `used` — подмножество, на которое опирается ответ; `used_missing_ids` — id из ответа, которых нет среди найденных (дефект engine, виден явно);
- `timings` — `engine.*` (этапы, измеренные engine), `backend_prepare`, `engine` (вызов целиком), `backend_persist`, `total`. Только реально измеренные значения;
- `error` — этап (`engine` / `backend_persist`), тип и замаскированное сообщение.

Маскирование (`backend/redaction.py`): `Authorization`/Bearer, `api_key=`, `token=`, `sk-…`, email, телефоны (`+7999***0001`), заголовки `X-Client-Token`/`X-Archive-Token`/`Cookie`. Применяется к трассам, сообщениям об ошибках и всем логгерам (включая uvicorn и тексты исключений).

## Замер скорости

```bash
python scripts/bench_latency.py --runs 5 --out reports/latency.json                  # in-process
python scripts/bench_latency.py --url http://localhost:8000 --runs 5                 # живой сервер
CONSULTATION_ENGINE=<модуль>:<Класс> python scripts/bench_latency.py --runs 5        # с настоящим engine
```

Отчёт: p50/p95/max по полному ответу и по этапам, доля ошибок, engine, git SHA. Ориентир организаторов 5–7 с не является жёстким timeout (QA09). Замер на заглушке помечается `is_stub_engine=true` и не публикуется как скорость системы.

Замер накладных расходов backend (заглушка, 24 хода): полный ответ p50 ≈ 3,4 мс, p95 ≈ 4,2 мс. Задержку системы определяют поиск и модель.
