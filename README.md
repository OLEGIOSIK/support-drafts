# Support drafts

Сервіс для підтримки: приймає звернення з хелпдеску, знаходить релевантні статті бази знань,
генерує чернетку відповіді через Claude, показує її оператору і після підтвердження надсилає
клієнту через API хелпдеску.

* **Стек:** Python 3.12, FastAPI, PostgreSQL 16, asyncpg, Anthropic SDK; `docker compose up`.
* **Продукт у базі знань:** вигаданий SaaS **Reportly** (звіти та дашборди) — 9 статей у `app/kb_articles/`.
* Рішення і компроміси: [docs/DESIGN.md](docs/DESIGN.md). Оригінальне завдання: [docs/TASK.md](docs/TASK.md).

## Швидкий старт

Потрібно: Docker з Compose v2.

```bash
git clone <repo-url> support-drafts && cd support-drafts
docker compose up --build
```

Готово, коли в логах `Uvicorn running on http://0.0.0.0:8000` і `worker started`.

| Що | URL |
|---|---|
| Інтерфейс оператора | http://localhost:8000/operator |
| Вебхук | `POST http://localhost:8000/webhooks/helpdesk` |
| Health | http://localhost:8000/health → `200 OK` |
| Відповіді, які «отримали» клієнти (мок хелпдеску) | http://localhost:8000/mock-helpdesk/replies |
| База знань | http://localhost:8000/kb |
| Swagger / JSON API | http://localhost:8000/docs, `/api/tickets`, `/api/stats` |

### Надіслати звернення

```bash
curl -i -X POST localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"evt_8f2a","ticket_id":"T-1042","customer_email":"user@example.com",
       "message":"Не можу експортувати звіт у PDF",
       "reply_url":"https://helpdesk.example.com/tickets/T-1042/replies"}'
```

Відповідь `202 {"status":"accepted"}`; повтор тієї ж події — `200 {"status":"duplicate"}`.
Через 5–30 с чернетка з'явиться в `/operator` (статус `draft_ready`). Відкрийте її, за потреби
відредагуйте і натисніть **Підтвердити** — відповідь піде в хелпдеск (мок), статус стане `sent`.

`reply_url` з хостом `helpdesk.example.com` автоматично спрямовується у вбудований мок
(`MOCK_HELPDESK_HOSTS`). Мок навмисно нестабільний: 20% відповідей `503`, 10% — «повільний успіх»
(зберіг, але відповів за 15 с, довше за наш таймаут). Сервіс повторює відправку з тим самим
`Idempotency-Key`, тож на `/mock-helpdesk/replies` дублікатів немає.

### Симуляція навантаження (дублікати, паралельні доставки, повтори)

```bash
docker compose exec app python scripts/simulate_helpdesk.py --count 500 --duration 600   # пік: 500 за 10 хв
docker compose exec app python scripts/simulate_helpdesk.py --count 50 --duration 10     # швидко
```

Скрипт поводиться як хелпдеск: таймаут 2 с, повтор доставки, 30% подій надсилає ще раз
трьома паралельними копіями. Прогрес: `/operator?status=all` або `GET /api/stats`.

## Режим з API-ключем і без

Без ключа сервіс повністю працює: робить **реальний** запит до `api.anthropic.com` (отримує 401),
а замість відповіді використовує статичну чернетку з посиланнями на знайдені статті та затримкою
5–30 с. У UI такі чернетки позначені `stub`.

Щоб генерувати справжні чернетки — створіть `.env` поруч із `docker-compose.yml`:

```bash
cp .env.example .env
# ANTHROPIC_API_KEY=sk-ant-...   (https://console.anthropic.com → API Keys)
docker compose up -d
```

## Тести

```bash
docker compose up -d db
docker compose run --rm -e DATABASE_URL=postgresql://support:support@db:5432/support_test app pytest -q
```

Покривають: дедуплікацію одночасних доставок, швидкість і безвтратність пачки вебхуків,
глобальний rate limiter під конкуренцією, режим без ключа (реальний запит + stub + затримка),
ретраї LLM і перманентні помилки, атомарність підтвердження, ретраї доставки з тим самим
`Idempotency-Key`, дедуплікацію в моку, перехоплення протермінованих lease, пошук по базі знань.

## Конфігурація

Усі змінні — в [.env.example](.env.example) та `app/config.py`. Основні:

| Змінна | За замовчуванням | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | без нього — режим stub |
| `LLM_MODEL` / `LLM_EFFORT` | `claude-opus-5-5` / `low` | |
| `LLM_RATE_LIMIT_PER_MIN` | `60` | глобально для всіх воркерів |
| `LLM_CONCURRENCY` | `30` | паралельних LLM-викликів на процес |
| `WEBHOOK_SECRET` | — | якщо задано, вебхук вимагає `X-Webhook-Secret` |
| `OPERATOR_PASSWORD` | — | якщо задано, Basic auth `operator:<пароль>` на `/operator`, `/api` |
| `REPLY_URL_ALLOWED_HOSTS` | — | allowlist хостів `reply_url` |
| `MOCK_FAIL_RATE` / `MOCK_SLOW_RATE` | `0.2` / `0.1` | нестабільність моку |
| `RUN_WORKER` | `false` | `true` — воркер усередині веб-процесу (для хостингу в один контейнер) |

Масштабування: `docker compose up --scale worker=3` — ліміт 60/хв і захоплення задач спільні через Postgres.

## Деплой (Render)

У репозиторії є `render.yaml` (Blueprint): веб-сервіс з вбудованим воркером + Postgres.
Render → **New → Blueprint** → вибрати репозиторій → **Apply**. Секрети (`ANTHROPIC_API_KEY`,
`OPERATOR_PASSWORD`, `WEBHOOK_SECRET`) можна лишити порожніми.
Безкоштовний інстанс засинає після 15 хв простою: перша доставка вебхука після сну може не вкластися
в 2 с — хелпдеск повторить її, і подія буде прийнята (дублікат відсіється).

## Структура

```
app/
  main.py           FastAPI, lifespan, /health
  webhook.py        POST /webhooks/helpdesk
  worker.py         генерація і доставка (черга в Postgres, leases)
  llm.py            Claude + режим без ключа
  rate_limit.py     глобальний ліміт 60/хв
  sender.py         POST reply_url з Idempotency-Key
  mock_helpdesk.py  мок хелпдеску
  operator.py       UI оператора, JSON API, сторінки бази знань
  kb.py, kb_articles/  база знань і BM25-пошук
  schema.sql, templates/
scripts/simulate_helpdesk.py
tests/
docs/DESIGN.md, docs/TASK.md, CLAUDE.md
```

## Як це робилось

Проєкт написано з Claude Code (агент). Конфігурація агента — [CLAUDE.md](CLAUDE.md); історія
комітів збережена як є.
