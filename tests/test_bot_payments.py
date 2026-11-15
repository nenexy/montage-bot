"""Сценарий оплаты в боте на поддельных объектах Telegram (без сети)."""

import asyncio
import importlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def bot_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAYMENT_PROVIDER_TOKEN", "123:TEST:abc")
    monkeypatch.setenv("PACKS", "1:99,5:399")
    monkeypatch.setenv("FREE_VIDEOS", "1")
    import bot

    mod = importlib.reload(bot)
    yield mod
    os.environ.pop("PAYMENT_PROVIDER_TOKEN", None)


def make_msg(uid=1, **kw):
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        chat=SimpleNamespace(id=uid),
        video=None, video_note=None, document=None, successful_payment=None,
        answer=AsyncMock(), answer_video=AsyncMock(),
    )
    for k, v in kw.items():
        setattr(msg, k, v)
    return msg


def video():
    return SimpleNamespace(file_size=1_000_000, duration=30)


def test_first_video_free_second_asks_to_pay(bot_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_mod, "run_job", AsyncMock(side_effect=lambda m, md, b, st, kind: calls.append(kind)))

    asyncio.run(bot_mod.on_video(make_msg(video=video()), bot=None))
    assert calls == ["free"]

    msg2 = make_msg(video=video())
    asyncio.run(bot_mod.on_video(msg2, bot=None))
    assert calls == ["free"]  # второй не запущен
    assert bot_mod.state(1).pending is msg2
    assert "Выбери пакет" in msg2.answer.call_args.args[0]


def test_pre_checkout_rejects_wrong_amount(bot_mod):
    q = SimpleNamespace(invoice_payload="pack:5:399", currency="RUB", total_amount=39900, answer=AsyncMock())
    asyncio.run(bot_mod.on_pre_checkout(q))
    assert q.answer.call_args.kwargs["ok"] is True

    q2 = SimpleNamespace(invoice_payload="pack:5:399", currency="RUB", total_amount=100, answer=AsyncMock())
    asyncio.run(bot_mod.on_pre_checkout(q2))
    assert q2.answer.call_args.kwargs["ok"] is False


def test_payment_credits_and_runs_pending_video(bot_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_mod, "run_job", AsyncMock(side_effect=lambda m, md, b, st, kind: calls.append((m, kind))))
    bot_mod.billing.try_spend(1)  # бесплатный уже потрачен
    pending = make_msg(video=video())
    bot_mod.state(1).pending = pending

    sp = SimpleNamespace(invoice_payload="pack:5:399", telegram_payment_charge_id="tg1",
                         provider_payment_charge_id="yk1", total_amount=39900, currency="RUB")
    paid = make_msg(successful_payment=sp)
    asyncio.run(bot_mod.on_paid(paid, bot=None))
    asyncio.run(bot_mod.on_paid(paid, bot=None))  # повторное уведомление — не зачислять дважды

    assert calls == [(pending, "paid")]
    assert bot_mod.billing.balance(1) == (0, 4)
    assert bot_mod.state(1).pending is None


def test_failed_montage_refunds(bot_mod, monkeypatch):
    def boom(*a, **k):
        raise ValueError("В файле нет видеодорожки")

    monkeypatch.setattr(bot_mod, "process", boom)
    monkeypatch.setattr(bot_mod, "probe", lambda p: SimpleNamespace(duration=10))
    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    msg = make_msg(video=video())
    msg.answer = AsyncMock(return_value=status)
    fake_bot = SimpleNamespace(download=AsyncMock())

    kind = bot_mod.billing.try_spend(1)
    asyncio.run(bot_mod.run_job(msg, msg.video, fake_bot, bot_mod.state(1), kind))

    assert bot_mod.billing.balance(1) == (1, 0)  # бесплатный ролик вернулся
    assert "вернул на баланс" in status.edit_text.call_args.args[0]
    assert not bot_mod.state(1).busy
