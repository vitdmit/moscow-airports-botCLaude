"""Тесты ключевых функций парсера.

Цель — поймать регрессии в самых хрупких местах:
 - классификация статуса (вылетел / задержан / отменён),
 - извлечение номеров рейсов и авиакомпаний из ячейки «Рейс»,
 - извлечение номера гейта из строки статуса.

Запуск:  python -m pytest tests/ -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.config import MSK
from src.models import ParsedStatus
from src.parser import (
    _classify_status,
    _extract_flight_numbers_and_airlines,
    _extract_gate,
)


@pytest.mark.parametrize("text,expected", [
    ("Выход на посадку 117", "117"),
    ("Стойки регистрации 350–354  Выход на посадку 101", "101"),
    ("Стойки регистрации 16–41  Выход на посадку 13", "13"),
    ("Выход на посадку D4", "D4"),
    ("Вылетел", None),
    ("Отменен", None),
])
def test_extract_gate(text, expected):
    assert _extract_gate(text) == expected


@pytest.mark.parametrize("text,expected_flights,expected_airlines", [
    ("FV 6519, SU 6519 Россия, Аэрофлот",
     ["FV 6519", "SU 6519"], ["Россия", "Аэрофлот"]),
    ("SU 1546 Аэрофлот",
     ["SU 1546"], ["Аэрофлот"]),
    ("J2 810, C7 4139 AZAL",
     ["J2 810", "C7 4139"], ["AZAL"]),
    ("U6 261 Уральские авиалинии",
     ["U6 261"], ["Уральские авиалинии"]),
])
def test_extract_flight_numbers(text, expected_flights, expected_airlines):
    f, a = _extract_flight_numbers_and_airlines(text)
    assert f == expected_flights
    assert a == expected_airlines


def _sched(hour: int, minute: int = 0):
    """Утилита: фиктивное scheduled datetime в MSK."""
    return datetime(2026, 5, 26, hour, minute, tzinfo=MSK)


def test_classify_departed():
    s, d = _classify_status("Вылетел", _sched(12))
    assert s == ParsedStatus.DEPARTED
    assert d is None


def test_classify_cancelled():
    s, d = _classify_status("Отменен", _sched(12))
    assert s == ParsedStatus.CANCELLED
    assert d is None


def test_classify_delayed_same_day():
    # рейс 18:30, задержан до 20:00 — те же сутки
    s, d = _classify_status("Вылет задержан до 20:00", _sched(18, 30))
    assert s == ParsedStatus.DELAYED_SAME_DAY
    assert d is not None and d.hour == 20 and d.minute == 0


def test_classify_delayed_next_day_by_month_token():
    # рейс 19:05, задержан до 01:30 4 апр — следующие сутки (явно указан день)
    s, d = _classify_status("Вылет задержан до 01:30 4 апр", _sched(19, 5))
    assert s == ParsedStatus.DELAYED_NEXT_DAY
    assert d is not None and d.hour == 1


def test_classify_delayed_next_day_by_time():
    # рейс 23:50, задержан до 01:30 — без указания даты, но время раньше
    # расписания => следующие сутки.
    s, d = _classify_status("Вылет задержан до 01:30", _sched(23, 50))
    assert s == ParsedStatus.DELAYED_NEXT_DAY
    assert d is not None


def test_classify_boarding_with_gate():
    s, d = _classify_status("Выход на посадку 117", _sched(19, 30))
    assert s == ParsedStatus.BOARDING
    assert d is None
