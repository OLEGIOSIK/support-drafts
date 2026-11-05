"""Draft generation with Claude (Anthropic Messages API).

No-key mode: the request is still sent to the provider for real (with a placeholder key,
so it gets 401). Its outcome is ignored and a static draft is returned after a random
5–30 s delay (measured from the start of the call), i.e. the rest of the system sees
the same latency profile as with a real model.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass

import anthropic

from app.config import settings
from app.kb import Article

log = logging.getLogger(__name__)

NO_KEY_PLACEHOLDER = "sk-ant-no-key-configured"

SYSTEM_PROMPT = """You are a customer support agent for Reportly, a SaaS tool for building \
business reports and dashboards. You write a DRAFT reply that a human operator will review \
before it is sent to the customer.

Rules:
- Reply in the same language the customer wrote in.
- Use only facts from the knowledge base articles provided. Do not invent features, prices, \
limits or steps that are not in the articles.
- Give concrete steps when the articles contain them. Keep it short: a greeting, the answer, \
an offer to help further.
- Link the articles you relied on as Markdown links [title](url), using the exact URLs given.
- If the articles do not answer the question, say politely that you have passed the request \
to a specialist who will follow up, and ask for any details that would help. Add no links then.
- Plain text with Markdown links only; no headings; no signature placeholders like [Name]."""


class LLMError(Exception):
    """Base error; `retryable` decides between retry with backoff and gen_failed."""

    retryable = True


class PermanentLLMError(LLMError):
    retryable = False


@dataclass
class DraftResult:
    text: str
    mode: str  # "real" | "stub"


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key or NO_KEY_PLACEHOLDER,
            timeout=settings.llm_timeout_s,
            max_retries=0,  # retries are ours: each one must pass the global rate limiter
        )
    return _client


def build_user_prompt(message: str, articles: list[Article]) -> str:
    if articles:
        kb = "\n\n".join(
            f'<article title="{a.title}" url="{a.url}">\n{a.body}\n</article>' for a in articles
        )
    else:
        kb = "(no relevant articles found)"
    return f"<knowledge_base>\n{kb}\n</knowledge_base>\n\n<customer_message>\n{message}\n</customer_message>"


def stub_draft(articles: list[Article]) -> str:
    if not articles:
        return (
            "Вітаємо! Дякуємо за звернення. Ми передали ваше питання профільному спеціалісту — "
            "він зв'яжеться з вами найближчим часом. Якщо можете, опишіть, будь ласка, кроки, "
            "після яких виникає проблема, і додайте скриншот."
        )
    links = "\n".join(f"- [{a.title}]({a.url})" for a in articles)
    return (
        "Вітаємо! Дякуємо за звернення. Ось інструкції, які допоможуть розв'язати питання:\n"
        f"{links}\n\n"
        "Якщо після цього щось не вийде — відповідайте на цей лист, і ми допоможемо."
    )


async def _call_provider(message: str, articles: list[Article]) -> anthropic.types.Message:
    return await client().messages.create(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(message, articles)}],
        output_config={"effort": settings.llm_effort},
        # On a safety-classifier decline, re-run on Anthropic's recommended fallback model.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )


async def generate_draft(message: str, articles: list[Article]) -> DraftResult:
    started = time.monotonic()

    if not settings.anthropic_api_key:
        try:
            await _call_provider(message, articles)
        except Exception as e:  # expected: 401; network errors are irrelevant in this mode too
            log.info("llm(no-key): provider answered %s; using static draft", type(e).__name__)
        target = random.uniform(settings.stub_delay_min_s, settings.stub_delay_max_s)
        await asyncio.sleep(max(0.0, target - (time.monotonic() - started)))
        return DraftResult(text=stub_draft(articles), mode="stub")

    try:
        resp = await _call_provider(message, articles)
    except anthropic.RateLimitError as e:  # 429
        raise LLMError(f"rate limited by provider: {e}") from e
    except anthropic.APITimeoutError as e:
        raise LLMError("provider timeout") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"connection error: {e}") from e
    except anthropic.InternalServerError as e:  # 5xx incl. 529 overloaded
        raise LLMError(f"provider {e.status_code}") from e
    except anthropic.APIStatusError as e:  # 400/401/403/404...: retry won't help
        raise PermanentLLMError(f"provider {e.status_code}: {e.message}") from e

    if resp.stop_reason == "refusal":
        raise PermanentLLMError("model declined to answer")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise LLMError(f"empty response (stop_reason={resp.stop_reason})")
    return DraftResult(text=text, mode="real")
