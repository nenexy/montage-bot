"""Баланс пользователей и платежи (SQLite). Без Telegram — чтобы легко тестировать."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pack:
    credits: int  # сколько роликов
    rub: int  # цена в рублях

    @property
    def payload(self) -> str:
        return f"pack:{self.credits}:{self.rub}"

    @property
    def title(self) -> str:
        return f"{self.credits} {plural(self.credits, 'ролик', 'ролика', 'роликов')}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def parse_packs(spec: str) -> list[Pack]:
    """'1:99,5:399,20:1290' → пакеты «кол-во роликов : цена в рублях»."""
    packs = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        credits, rub = item.split(":")
        pack = Pack(int(credits), int(rub))
        if pack.credits <= 0 or pack.rub < 60:  # у Telegram минимальный платёж ~60 ₽ (≈1 $)
            raise ValueError(f"Неверный пакет {item!r}: нужно ≥1 ролика и ≥60 ₽")
        packs.append(pack)
    return packs


def find_pack(packs: list[Pack], payload: str) -> Pack | None:
    return next((p for p in packs if p.payload == payload), None)


def receipt_data(pack: Pack, vat_code: int = 1) -> str:
    """Данные для чека ЮKassa по 54-ФЗ (vat_code=1 — без НДС, подходит самозанятым и УСН)."""
    return json.dumps(
        {
            "receipt": {
                "items": [
                    {
                        "description": f"Автомонтаж видео: {pack.title}"[:128],
                        "quantity": "1.00",
                        "amount": {"value": f"{pack.rub:.2f}", "currency": "RUB"},
                        "vat_code": vat_code,
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ]
            }
        },
        ensure_ascii=False,
    )


class Billing:
    def __init__(self, path: Path, free_videos: int = 1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.free_videos = free_videos
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                free_left INTEGER NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                videos_done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,          -- telegram_payment_charge_id
                provider_charge_id TEXT,             -- id платежа в ЮKassa (для возвратов)
                uid INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                amount INTEGER NOT NULL,             -- в копейках
                currency TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

    def _ensure(self, uid: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO users (uid, free_left) VALUES (?, ?)", (uid, self.free_videos)
        )

    def balance(self, uid: int) -> tuple[int, int]:
        """(бесплатных осталось, оплаченных роликов)."""
        with self._lock:
            self._ensure(uid)
            row = self._db.execute("SELECT free_left, credits FROM users WHERE uid=?", (uid,)).fetchone()
            return row[0], row[1]

    def try_spend(self, uid: int) -> str | None:
        """Списывает один ролик. Возвращает 'free' / 'paid' или None, если баланса нет."""
        with self._lock:
            self._ensure(uid)
            if self._db.execute(
                "UPDATE users SET free_left = free_left - 1 WHERE uid=? AND free_left > 0", (uid,)
            ).rowcount:
                return "free"
            if self._db.execute(
                "UPDATE users SET credits = credits - 1 WHERE uid=? AND credits > 0", (uid,)
            ).rowcount:
                return "paid"
            return None

    def refund(self, uid: int, kind: str) -> None:
        """Возвращает ролик на баланс, если монтаж не удался."""
        column = "free_left" if kind == "free" else "credits"
        with self._lock:
            self._db.execute(f"UPDATE users SET {column} = {column} + 1 WHERE uid=?", (uid,))

    def mark_done(self, uid: int) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET videos_done = videos_done + 1 WHERE uid=?", (uid,))

    def add_payment(
        self, charge_id: str, provider_charge_id: str, uid: int, credits: int, amount: int, currency: str
    ) -> bool:
        """Зачисляет оплату. False, если этот платёж уже был зачислен (повторное уведомление)."""
        with self._lock:
            self._ensure(uid)
            try:
                self._db.execute("BEGIN")
                self._db.execute(
                    "INSERT INTO payments (charge_id, provider_charge_id, uid, credits, amount, currency) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (charge_id, provider_charge_id, uid, credits, amount, currency),
                )
                self._db.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (credits, uid))
                self._db.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                return False

    def stats(self) -> dict[str, int]:
        with self._lock:
            users, done = self._db.execute("SELECT COUNT(*), COALESCE(SUM(videos_done),0) FROM users").fetchone()
            paid, revenue = self._db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments").fetchone()
            return {"users": users, "videos": done, "payments": paid, "revenue_rub": revenue // 100}
