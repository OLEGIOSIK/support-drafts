import pytest

from app import kb


@pytest.fixture(autouse=True)
def _load():
    kb.load()


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Не можу експортувати звіт у PDF", "export-pdf"),
        ("Забув пароль, не можу увійти", "password-2fa"),
        ("Как пригласить коллегу в команду?", "team-roles"),
        ("Дані не оновлюються, помилка синхронізації", "data-sync"),
        ("Need an invoice for my payment", "billing"),
        ("How to embed dashboard with iframe", "dashboard-sharing"),
        ("Не могу выгрузить в Excel", "export-excel-csv"),
    ],
)
def test_top_result(query, expected):
    results = kb.search(query)
    assert results, query
    assert results[0][0].slug == expected


def test_irrelevant_query_returns_nothing():
    assert kb.search("погода у Києві завтра") == []


def test_stemmer_merges_inflections():
    assert kb.stem("звіту") == kb.stem("звіт")
    assert kb.stem("експортувати") == kb.stem("експорт")
