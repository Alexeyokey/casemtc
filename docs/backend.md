# Backend: запуск, хранение, архив, debug (S2–S4)

## Запуск

```bash
pip install -r requirements.lock -r requirements-dev.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000      # один воркер
pytest -q                                                 # тесты
```

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `CONSULTATION_ENGINE` | `core.stub_engine:StubEngine` | Явное переопределение реализации (`module:Factory`) |
| `ENERGYTOOL_KB_PATH` | — | При заданном архиве подключает `RagEngine`, если `CONSULTATION_ENGINE` не задан |
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
- `leads.session_id` уникален: одна заявка на диалог. Подтверждение нового предпросмотра увеличивает `revision`. Для повтора того же подтверждения используются исходные `offer_id` и `idempotency_key`.

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

В трассах, сообщениях об ошибках и стандартных журналах backend маскируются ключи, токены, email, телефоны и чувствительные HTTP-заголовки. `Cookie` и `Set-Cookie` скрываются целиком, включая значения отдельных cookies. В журналах обрабатываются вложенные аргументы, форматированные сообщения и traceback. Формат записей uvicorn сохраняется.

По токену сессии чат получает метаданные источников, время и тип сбоя. Текст фрагментов и сообщение внутренней ошибки доступны в debug по токену сессии вместе с `X-Archive-Token`, а также в локальном экспорте с `--traces`. Токен архива хранится на сервере и передаётся проверяющему отдельно от интерфейса.

## Замер скорости

```bash
python scripts/bench_latency.py --runs 5 --out reports/latency.json                  # in-process
python scripts/bench_latency.py --url http://localhost:8000 --runs 5 --out reports/latency.json
```

В отчёте есть p50/p95/max для полного ответа, сохранения заявки и этапов из доступных трасс. Перцентили рассчитаны методом ближайшего ранга. Доля ошибок рассчитана по попыткам открыть сессию, отправить реплику, получить предпросмотр и сохранить заявку. Запросы отладки считаются отдельно. Скрипт создаёт тестовые сессии и заявку с вымышленным номером, поэтому запускайте его на тестовом экземпляре.

`target_build_sha` приходит из `/api/v1/health` (`CASEMTC_BUILD_SHA` на сервере). Если сервер его не сообщает, известный SHA можно передать через `--target-build-sha`. При отсутствии обоих значений поле будет `null`, а `target_build_sha_source` — `unknown`. `benchmark_git_sha` относится к checkout, из которого запущен скрипт.

Ориентир организаторов составляет 5–7 с (QA09). Замер на заглушке помечается `is_stub_engine=true` и показывает накладные расходы backend.
