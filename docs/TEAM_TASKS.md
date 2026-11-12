# Командный план МТС Energytool

План разработки, а не отчёт о готовом решении. Подготовлен по кейсу, регламенту, материалам и [уточнениям Q&A организаторов](ORGANIZER_QA.md). Сырые материалы в этот репозиторий не загружены.

## Распределение

26 инженерных задач, всего 62 SP. SP отражают предварительную относительную сложность. После первых PR переоцениваем объём и обновляем план.

| Участник | Зона ответственности | SP | Задачи |
|---|---|---:|---|
| Влад | Независимая проверка поиска, сравнение вариантов и ревью кода с другом в Claude | 7 | [R4](https://github.com/Alexeyokey/casemtc/issues/18), [R5](https://github.com/Alexeyokey/casemtc/issues/22), [R6](https://github.com/Alexeyokey/casemtc/issues/31) |
| Максим | Состояние консультации, контекст, генерация, проверка фактов и диалоговое качество | 12 | [C1](https://github.com/Alexeyokey/casemtc/issues/6), [C2](https://github.com/Alexeyokey/casemtc/issues/10), [C3](https://github.com/Alexeyokey/casemtc/issues/14), [C4](https://github.com/Alexeyokey/casemtc/issues/19), [C5](https://github.com/Alexeyokey/casemtc/issues/23) |
| Алексей | Обработка документов, реализация поиска, API, CSV, ZIP, Docker/CI и комплект сдачи | 19 | [R1](https://github.com/Alexeyokey/casemtc/issues/3), [R2](https://github.com/Alexeyokey/casemtc/issues/7), [R3](https://github.com/Alexeyokey/casemtc/issues/11), [I1](https://github.com/Alexeyokey/casemtc/issues/4), [I2](https://github.com/Alexeyokey/casemtc/issues/9), [I3](https://github.com/Alexeyokey/casemtc/issues/16), [I4](https://github.com/Alexeyokey/casemtc/issues/17), [I5](https://github.com/Alexeyokey/casemtc/issues/26) |
| Женя | Общие контракты, backend, сессии/архив, заявки, debug и сквозная приёмка | 12 | [S1](https://github.com/Alexeyokey/casemtc/issues/2), [S2](https://github.com/Alexeyokey/casemtc/issues/8), [S3](https://github.com/Alexeyokey/casemtc/issues/12), [S4](https://github.com/Alexeyokey/casemtc/issues/20), [S5](https://github.com/Alexeyokey/casemtc/issues/25) |
| Катрина | Каркас web, живой чат, форма заявки, debug/архив UI, адаптивность и UI QA | 12 | [U1](https://github.com/Alexeyokey/casemtc/issues/5), [U2](https://github.com/Alexeyokey/casemtc/issues/13), [U3](https://github.com/Alexeyokey/casemtc/issues/15), [U4](https://github.com/Alexeyokey/casemtc/issues/21), [U5](https://github.com/Alexeyokey/casemtc/issues/24) |

## Все задачи и зависимости

| ID | Ответственный | Приоритет | SP | Этап | Задача | Зависит от |
|---|---|---|---:|---|---|---|
| S1 | Женя | P0 | 2 | M0 | [#2 Общие контракты ядра, API и тестовые примеры](https://github.com/Alexeyokey/casemtc/issues/2) | Можно начинать |
| R1 | Алексей | P0 | 2 | M0 | [#3 Инвентаризация корпуса и безопасный загрузчик БЗ](https://github.com/Alexeyokey/casemtc/issues/3) | Можно начинать |
| I1 | Алексей | P0 | 2 | M0 | [#4 Клиент API организаторов и конфигурация окружения](https://github.com/Alexeyokey/casemtc/issues/4) | Можно начинать |
| U1 | Катрина | P0 | 2 | M0 | [#5 Каркас веб-чата и работа на контрактных моках](https://github.com/Alexeyokey/casemtc/issues/5) | Можно начинать |
| C1 | Максим | P0 | 2 | M0 | [#6 Состояние консультации и выбор следующего действия](https://github.com/Alexeyokey/casemtc/issues/6) | [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| R2 | Алексей | P0 | 2 | M1 | [#7 Разбиение Markdown, таблиц и FAQ на доказательные фрагменты](https://github.com/Alexeyokey/casemtc/issues/7) | [R1](https://github.com/Alexeyokey/casemtc/issues/3), [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| S2 | Женя | P0 | 3 | M1 | [#8 Сессии, полный архив и изоляция контекста](https://github.com/Alexeyokey/casemtc/issues/8) | [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| I2 | Алексей | P0 | 3 | M1 | [#9 solution.py: чтение CSV, независимые диалоги и запись ответов](https://github.com/Alexeyokey/casemtc/issues/9) | [I1](https://github.com/Alexeyokey/casemtc/issues/4), [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| C2 | Максим | P0 | 3 | M1 | [#10 Понимание реплики, контекст темы и запрос к поиску](https://github.com/Alexeyokey/casemtc/issues/10) | [C1](https://github.com/Alexeyokey/casemtc/issues/6), [I1](https://github.com/Alexeyokey/casemtc/issues/4) |
| R3 | Алексей | P0 | 3 | M1 | [#11 Поиск по всей БЗ и подбор подтверждающих источников](https://github.com/Alexeyokey/casemtc/issues/11) | [R2](https://github.com/Alexeyokey/casemtc/issues/7) |
| S3 | Женя | P0 | 3 | M1 | [#12 Сбор заявки, исправление контактов и резюме менеджеру](https://github.com/Alexeyokey/casemtc/issues/12) | [S2](https://github.com/Alexeyokey/casemtc/issues/8), [C1](https://github.com/Alexeyokey/casemtc/issues/6) |
| U2 | Катрина | P0 | 3 | M1 | [#13 Подключение живого диалога, сброс и восстановление после ошибки](https://github.com/Alexeyokey/casemtc/issues/13) | [U1](https://github.com/Alexeyokey/casemtc/issues/5), [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| C3 | Максим | P0 | 3 | M1 | [#14 Генерация консультации с источниками и продуктовой применимостью](https://github.com/Alexeyokey/casemtc/issues/14) | [C2](https://github.com/Alexeyokey/casemtc/issues/10), [R3](https://github.com/Alexeyokey/casemtc/issues/11) |
| U3 | Катрина | P0 | 3 | M1 | [#15 Форма контактов, предпросмотр и результат заявки](https://github.com/Alexeyokey/casemtc/issues/15) | [U1](https://github.com/Alexeyokey/casemtc/issues/5), [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| I3 | Алексей | P0 | 3 | M1 | [#16 Экспорт платформенного ZIP и проверка контракта запуска](https://github.com/Alexeyokey/casemtc/issues/16) | [I2](https://github.com/Alexeyokey/casemtc/issues/9), [C3](https://github.com/Alexeyokey/casemtc/issues/14) |
| I4 | Алексей | P0 | 2 | M1 | [#17 Docker-запуск, env, зависимости и базовый CI](https://github.com/Alexeyokey/casemtc/issues/17) | [I1](https://github.com/Alexeyokey/casemtc/issues/4), [S1](https://github.com/Alexeyokey/casemtc/issues/2) |
| R4 | Влад | P1 | 3 | M2 | [#18 Сравнение качества и скорости вариантов поиска](https://github.com/Alexeyokey/casemtc/issues/18) | [R5](https://github.com/Alexeyokey/casemtc/issues/22) и доступные сборки. Базовый замер можно начать сейчас |
| C4 | Максим | P0 | 2 | M2 | [#19 Контроль фактов, границы компетенции и prompt injection](https://github.com/Alexeyokey/casemtc/issues/19) | [C3](https://github.com/Alexeyokey/casemtc/issues/14) |
| S4 | Женя | P0 | 2 | M2 | [#20 Отладочные трассы и безопасное наблюдение за запросом](https://github.com/Alexeyokey/casemtc/issues/20) | [S2](https://github.com/Alexeyokey/casemtc/issues/8), [R3](https://github.com/Alexeyokey/casemtc/issues/11), [C3](https://github.com/Alexeyokey/casemtc/issues/14) |
| U4 | Катрина | P0 | 2 | M2 | [#21 Режим отладки и доступ к сохранённым диалогам](https://github.com/Alexeyokey/casemtc/issues/21) | [U2](https://github.com/Alexeyokey/casemtc/issues/13), [S2](https://github.com/Alexeyokey/casemtc/issues/8), [S4](https://github.com/Alexeyokey/casemtc/issues/20) |
| R5 | Влад | P0 | 2 | M2 | [#22 Независимая проверка поиска по контрольным вопросам](https://github.com/Alexeyokey/casemtc/issues/22) | [Базовая сборка #30](https://github.com/Alexeyokey/casemtc/pull/30) |
| R6 | Влад | P1 | 2 | M2 | [#31 Независимое ревью поиска в Claude с помощью друга](https://github.com/Alexeyokey/casemtc/issues/31) | [Код из #30](https://github.com/Alexeyokey/casemtc/pull/30) или следующая версия Алексея |
| C5 | Максим | P0 | 2 | M2 | [#23 Оценка качества консультации и диалоговые регрессии](https://github.com/Alexeyokey/casemtc/issues/23) | [C4](https://github.com/Alexeyokey/casemtc/issues/19), [S2](https://github.com/Alexeyokey/casemtc/issues/8) |
| U5 | Катрина | P1 | 2 | M2 | [#24 Мобильная проверка, доступность и материалы показа](https://github.com/Alexeyokey/casemtc/issues/24) | [U2](https://github.com/Alexeyokey/casemtc/issues/13), [U3](https://github.com/Alexeyokey/casemtc/issues/15), [U4](https://github.com/Alexeyokey/casemtc/issues/21) |
| S5 | Женя | P0 | 2 | M2 | [#25 Сквозная приёмка, сохранность и сценарий внедрения](https://github.com/Alexeyokey/casemtc/issues/25) | [S3](https://github.com/Alexeyokey/casemtc/issues/12), [S4](https://github.com/Alexeyokey/casemtc/issues/20), [I4](https://github.com/Alexeyokey/casemtc/issues/17), [U3](https://github.com/Alexeyokey/casemtc/issues/15), [U4](https://github.com/Alexeyokey/casemtc/issues/21), [C4](https://github.com/Alexeyokey/casemtc/issues/19) |
| I5 | Алексей | P0 | 2 | M2 | [#26 Платформенный прогон, бюджет времени и финальный комплект](https://github.com/Alexeyokey/casemtc/issues/26) | [I3](https://github.com/Alexeyokey/casemtc/issues/16), [I4](https://github.com/Alexeyokey/casemtc/issues/17), [R5](https://github.com/Alexeyokey/casemtc/issues/22), [C5](https://github.com/Alexeyokey/casemtc/issues/23), [S5](https://github.com/Alexeyokey/casemtc/issues/25), [U5](https://github.com/Alexeyokey/casemtc/issues/24) |

## Порядок работы

1. На M0 Женя фиксирует контракты, Алексей исследует структуру данных и готовит клиент API, Катрина собирает интерфейс на тестовых ответах. Максим после первого контракта начинает работу над состоянием диалога.
2. M1: получаем два потребителя одного ядра — CSV и web — и путь от вопроса до заявки. Соседние блоки сначала работают на контрактных fixtures.
3. M2: качество поиска/ответов, архив/debug, ошибки, мобильный сценарий, реальный допустимый сабмит, пакет сдачи. P1 не подменяет обязательную приёмку P0.

Подробности и checkboxes — внутри Issues. `status:ready`/`status:blocked` показывают, можно ли начать работу. После завершения зависимостей метки обновляются вручную. Процесс закрытия — проверенный PR, затем закрытие issue; не закрывать задачу по одному наличию файла.

## Общие решения и границы

- Одно ядро консультации с явной историей; web и solution.py — адаптеры. Архив не является памятью нового диалога.
- Полезная консультация доступна до контактов. Для заявки запрашиваем ФИО/телефон/компанию, допускаем сохранение с одним валидным телефоном при запросе обратной связи; без контакта остаются консультация и архив. Цена и обещания требуют подтверждения из выданной БЗ.
- Максим отвечает за консультацию и проверку фактов. Алексей ведёт обработку документов, реализацию поиска, внешний API и платформенную упаковку. Влад проверяет выдачу и фиксирует замечания. Женя отвечает за HTTP и хранение, Катрина — за интерфейс.
- Генеративная модель и API-конфигурация читаются из окружения организаторов. Личная подписка Codex не является зависимостью проекта.
- Архив БЗ не выполняется как код. Закрытые данные, фрагменты, индексы и ключи не коммитятся; тесты в Git используют синтетические документы.
- Владельцы исправляют собственные интеграционные дефекты. Передача блока не означает передачу всех его багов Алексею.
- Итоговая защита общая. Влад представляет результаты проверки поиска, Максим — консультацию и качество, Женя — внедрение, заявки и надёжность, Катрина — пользовательский путь, Алексей — реализацию поиска, запуск, платформу и PDF.
- Общая приёмка и обзор всех локальных веток/worktree обязательны до фиксации сдачи. Тег, порт и видео из прежнего хакатона не становятся требованиями этого кейса автоматически.

## Что изменили после Q&A

- Индекс устно разрешён в корне ZIP (41:49–42:32). R3 готовит загрузку и построение при старте. R4 оценивает доступные варианты. I3 отвечает за профили `runtime` и `precomputed`. I5 проверяет перенос файлов платформой. Совместимость готового индекса с загрузчиком платформы ещё предстоит проверить.
- Заявка с телефоном может сохраняться без компании/региона; ФИО/компанию предлагаем, не выдумываем. Полный отказ от контактов не прекращает помощь. S1/C1/S3/U3 и сквозные проверки согласованы с этой логикой.
- Для менеджера достаточно сохранённых контактов и актуального человекочитаемого резюме, без обязательной CRM/почты. Для архива достаточно проверяемого локального хранения/экспорта; отдельный архивный экран — улучшение.
- В обычном ответе нет сырых файлов/rules; документы и timings — в переключаемом debug. Отдельная роль сотрудника МТС не требуется.
- 5–7 секунд — ориентир скорости, а не жёсткий timeout. Демо — примеры, детализация скрытого судьи не обещана. Архитектуру усложняем только по измерениям.
- Детали и временные отметки: [ORGANIZER_QA.md](ORGANIZER_QA.md). Текущее распределение отражено в таблицах выше.

## Что ещё проверить

1. I3/I5: текущая платформа действительно переносит и читает корневые файлы индекса; допустимые расширения и метаданные. Устное разрешение уже есть; до доказанного прогона доступен профиль runtime.
2. I1: фактический API-контракт параметров, batch/parallel behaviour и ошибки при получении организаторского доступа. Дополнительные квоты устно не объявлены; конечные повторы/таймауты и бюджет клиента сохраняются. Отдельный тестовый доступ не обещан.
3. S1/S2/I2: фактический формат истории, границы закрытия/нового диалога и способ доступа проверяющего к локальному архиву. Отдельный архивный кабинет и возобновление активной сессии после restart не объявлены обязательными.
4. I5: окончательные правила выбора private-сабмита, программа защиты, дедлайн и доступ жюри. Неясные устные цифры не меняют письменные лимиты; способы обработки закрытого корпуса и allowlist сверяем с действующими материалами.

Вопросы фиксируем в repo и выносим организаторам вручную; сообщения от лица команды автоматически не отправляются. Дедлайн не указан, даты milestones не придуманы. GitHub-аккаунт подтверждён только для Алексея: Alexeyokey. Остальные ответственные обозначены именами и owner-метками; неизвестные логины не назначаем по предположению.
