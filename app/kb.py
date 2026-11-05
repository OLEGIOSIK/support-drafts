"""Knowledge base: markdown files in app/kb_articles, searched with in-memory BM25.

Why not a vector DB: the KB is ~10 short articles; BM25 over crude stems is
deterministic, needs no extra service or embedding calls (which would compete with
the 60 req/min LLM budget) and handles Ukrainian/Russian/English inflection well
enough thanks to prefix-stemming and multilingual `keywords` in front matter.
For a large KB the next step is pgvector + embeddings.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

ARTICLES_DIR = Path(__file__).with_name("kb_articles")
TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)
STOPWORDS = {
    # uk / ru / en, minimal
    "не", "і", "й", "та", "в", "у", "на", "з", "із", "до", "що", "як", "це", "я", "ми", "ви",
    "мені", "мій", "моя", "можу", "можна", "чому", "або", "за", "для", "по", "от", "и", "что",
    "как", "это", "мне", "могу", "почему", "или", "the", "a", "an", "to", "of", "and", "is",
    "i", "my", "can", "can't", "how", "in", "on", "for", "it", "not", "do", "does", "why",
}
FIELD_WEIGHTS = {"title": 3, "keywords": 3, "body": 1}
K1, B = 1.4, 0.75


def stem(token: str) -> str:
    """Crude multilingual stemmer: cut endings by keeping a prefix (звіту/звіт, експортувати/експорт)."""
    t = token.lower().replace("ё", "е").replace("'", "")
    if len(t) <= 4:
        return t
    return t[: max(4, min(len(t) - 1, 6))]


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


@dataclass
class Article:
    slug: str
    title: str
    keywords: str
    body: str
    tf: Counter = field(default_factory=Counter)
    length: int = 0

    @property
    def url(self) -> str:
        return f"{settings.public_url}/kb/{self.slug}"


_articles: dict[str, Article] = {}
_df: Counter = Counter()
_avg_len: float = 1.0


def _parse(path: Path) -> Article:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        _, header, body = raw.split("---", 2)
        for line in header.strip().splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return Article(
        slug=path.stem,
        title=meta.get("title", path.stem),
        keywords=meta.get("keywords", ""),
        body=body.strip(),
    )


def load(directory: Path = ARTICLES_DIR) -> None:
    global _avg_len
    _articles.clear()
    _df.clear()
    for path in sorted(directory.glob("*.md")):
        art = _parse(path)
        tf: Counter = Counter()
        for fname, weight in FIELD_WEIGHTS.items():
            for tok in tokenize(getattr(art, fname)):
                tf[tok] += weight
        art.tf, art.length = tf, sum(tf.values())
        _articles[art.slug] = art
        _df.update(tf.keys())
    _avg_len = sum(a.length for a in _articles.values()) / max(len(_articles), 1)


def all_articles() -> list[Article]:
    if not _articles:
        load()
    return list(_articles.values())


def get(slug: str) -> Article | None:
    if not _articles:
        load()
    return _articles.get(slug)


def search(query: str, limit: int = 3, min_relative: float = 0.35) -> list[tuple[Article, float]]:
    """BM25 top-N; drops results much weaker than the best one to avoid noisy links."""
    if not _articles:
        load()
    n = len(_articles)
    q_terms = set(tokenize(query))
    scored = []
    for art in _articles.values():
        score = 0.0
        for term in q_terms:
            f = art.tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - _df[term] + 0.5) / (_df[term] + 0.5))
            score += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * art.length / _avg_len))
        if score > 0:
            scored.append((art, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    if not scored:
        return []
    best = scored[0][1]
    return [(a, s) for a, s in scored[:limit] if s >= best * min_relative]
