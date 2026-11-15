import json

import pytest

from billing import Billing, Pack, find_pack, parse_packs, receipt_data


@pytest.fixture
def billing(tmp_path):
    return Billing(tmp_path / "bot.db", free_videos=1)


def test_free_then_paid_then_empty(billing):
    assert billing.balance(1) == (1, 0)
    assert billing.try_spend(1) == "free"
    assert billing.try_spend(1) is None

    assert billing.add_payment("ch1", "yk1", 1, 5, 39900, "RUB")
    assert billing.balance(1) == (0, 5)
    assert billing.try_spend(1) == "paid"
    assert billing.balance(1) == (0, 4)


def test_duplicate_payment_not_credited_twice(billing):
    assert billing.add_payment("ch1", "yk1", 1, 5, 39900, "RUB")
    assert not billing.add_payment("ch1", "yk1", 1, 5, 39900, "RUB")
    assert billing.balance(1) == (1, 5)
    assert billing.stats()["payments"] == 1
    assert billing.stats()["revenue_rub"] == 399


def test_refund_returns_same_kind(billing):
    billing.add_payment("ch1", "yk1", 1, 1, 9900, "RUB")
    kind = billing.try_spend(1)
    assert kind == "free"
    billing.refund(1, kind)
    assert billing.balance(1) == (1, 1)
    billing.try_spend(1)
    kind = billing.try_spend(1)
    assert kind == "paid"
    billing.refund(1, kind)
    assert billing.balance(1) == (0, 1)


def test_balance_persists(tmp_path):
    Billing(tmp_path / "bot.db").add_payment("ch1", "yk1", 7, 3, 29900, "RUB")
    assert Billing(tmp_path / "bot.db").balance(7) == (1, 3)


def test_parse_packs_and_lookup():
    packs = parse_packs("1:99, 5:399")
    assert packs == [Pack(1, 99), Pack(5, 399)]
    assert find_pack(packs, "pack:5:399") == Pack(5, 399)
    assert find_pack(packs, "pack:5:1") is None  # подделанная цена не пройдёт
    with pytest.raises(ValueError):
        parse_packs("1:10")


def test_receipt_amount_matches_price():
    data = json.loads(receipt_data(Pack(5, 399)))
    item = data["receipt"]["items"][0]
    assert item["amount"] == {"value": "399.00", "currency": "RUB"}
    assert item["vat_code"] == 1
