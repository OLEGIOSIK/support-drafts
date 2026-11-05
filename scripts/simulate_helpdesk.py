"""Load/robustness simulator: behaves like the helpdesk delivering webhooks.

* waits at most 2 s for the webhook and re-delivers on timeout / 5xx (like the real one);
* re-sends some events again (duplicates), some of them concurrently;
* reply_url points to helpdesk.example.com, which the service routes to its built-in mock.

    python scripts/simulate_helpdesk.py --url http://localhost:8000 --count 500 --duration 600
    docker compose exec app python scripts/simulate_helpdesk.py --url http://localhost:8000 --count 50
"""

import argparse
import asyncio
import random
import time
import uuid

import httpx

MESSAGES = [
    "Не можу експортувати звіт у PDF, кнопка неактивна",
    "Як налаштувати щотижневу розсилку звіту на пошту?",
    "Забув пароль і не приходить лист для скидання",
    "Дані в дашборді не оновлюються вже другий день",
    "Як підключити PostgreSQL? Пише connection timed out",
    "Потрібен рахунок з реквізитами компанії для бухгалтерії",
    "Как пригласить коллегу и дать ему только просмотр?",
    "Не могу выгрузить таблицу в Excel, файл открывается иероглифами",
    "How do I embed a dashboard on our website?",
    "I lost my phone with the 2FA app, how can I log in?",
    "Хочу скасувати підписку, чи повернете гроші?",
    "Чи є у вас інтеграція з 1С?",
]


async def deliver(client: httpx.AsyncClient, url: str, event: dict, stats: dict, secret: str | None):
    headers = {"X-Webhook-Secret": secret} if secret else {}
    for attempt in range(1, 20):
        try:
            r = await client.post(f"{url}/webhooks/helpdesk", json=event, headers=headers, timeout=2.0)
            if r.status_code < 500:
                stats[r.status_code] = stats.get(r.status_code, 0) + 1
                return
            stats["5xx"] = stats.get("5xx", 0) + 1
        except httpx.TimeoutException:
            stats["timeout"] = stats.get("timeout", 0) + 1
        except httpx.TransportError:
            stats["conn_error"] = stats.get("conn_error", 0) + 1
        await asyncio.sleep(min(2 ** attempt, 30) * random.uniform(0.5, 1.0))
    stats["gave_up"] = stats.get("gave_up", 0) + 1


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--count", type=int, default=30, help="unique events")
    ap.add_argument("--duration", type=float, default=10, help="spread events over N seconds")
    ap.add_argument("--dup-rate", type=float, default=0.3, help="share of events re-delivered")
    ap.add_argument("--concurrent-dups", type=int, default=3, help="parallel copies for a duplicate")
    ap.add_argument("--secret", default=None)
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    stats: dict = {}
    tasks = []
    t0 = time.monotonic()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200)) as client:
        for i in range(args.count):
            ticket = f"T-{run}-{i}"
            event = {
                "event_id": f"evt_{run}_{i}",
                "ticket_id": ticket,
                "customer_email": f"user{i}@example.com",
                "message": random.choice(MESSAGES),
                "reply_url": f"https://helpdesk.example.com/tickets/{ticket}/replies",
            }
            copies = args.concurrent_dups if random.random() < args.dup_rate else 1
            for _ in range(copies):
                tasks.append(asyncio.create_task(deliver(client, args.url, event, stats, args.secret)))
            await asyncio.sleep(args.duration / max(args.count, 1))
        await asyncio.gather(*tasks)
    print(f"run={run} unique_events={args.count} deliveries={len(tasks)} "
          f"elapsed={time.monotonic() - t0:.1f}s results={stats}")


if __name__ == "__main__":
    asyncio.run(main())
