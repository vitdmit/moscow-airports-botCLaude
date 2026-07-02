"""Ретроактивное дополнение данных за месяц (все 3 аэропорта).

Обновляет существующие CSV в data/daily/ без затрат бюджета AeroDataBox:
  1. Добор РЕЙСОВ из снапшота живого табло по SVO/VKO/DME (то, что
     AeroDataBox не отдал — напр. при частичном сборе дня или пропуске
     аэропорта из-за исчерпания бюджета). Кодшеринги склеиваются в одну
     строку — логика общая с daily_fetch (add_missing_flights_from_snapshot).
  2. Обогащение гейтами DME из имеющихся gate_snapshots.
  3. Добавление пропущенных рейсов DME из исторического Яндекс.Расписания.

НОЛЬ затрат бюджета AeroDataBox. Перезаписывает CSV только если что-то изменилось.

Параметры (env-переменные или GitHub Actions inputs):
  BACKFILL_MONTH   YYYY-MM     месяц для обработки (по умолчанию: прошлый)
  BACKFILL_FROM    YYYY-MM-DD  начать с этой даты (приоритет над MONTH)
  BACKFILL_TO      YYYY-MM-DD  закончить этой датой (используется вместе с FROM)

Запуск:
  Через GitHub Actions: Actions → «Бэкфилл DME» → Run workflow
  Вручную из корня репо: python scripts/backfill_dme.py

Пример: перегнать весь июнь одним разом
  BACKFILL_MONTH=2026-06 python scripts/backfill_dme.py
"""
from __future__ import annotations

import csv
import os
import sys
import time as time_module
from datetime import date, timedelta
from pathlib import Path

# Добавляем корень проекта в sys.path (нужно при запуске как скрипта)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from src.config import DAILY_DIR
from src.daily_fetch import (
    fill_dme_gates, write_csv, add_missing_flights_from_snapshot,
)
from src.utils import get_logger

log = get_logger("backfill_dme")

# Аэропорты, для которых пробуем добор рейсов из снапшота табло
AIRPORTS_BACKFILL = ("SVO", "VKO", "DME")

# Пауза между Яндекс-запросами: меньше — быстрее, но выше риск блокировки
_YANDEX_DELAY_SEC = 4


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _month_range(month_str: str) -> tuple[date, date]:
    """'2026-06' → (2026-06-01, 2026-06-30)."""
    y, m = (int(x) for x in month_str.split("-"))
    start = date(y, m, 1)
    end = date(y, m + 1, 1) - timedelta(days=1) if m < 12 \
        else date(y + 1, 1, 1) - timedelta(days=1)
    return start, end


def _prev_month() -> str:
    today = date.today()
    return f"{today.year}-{today.month - 1:02d}" if today.month > 1 \
        else f"{today.year - 1}-12"


def _counts(rows: list[dict]) -> dict[str, int]:
    c = {ap: 0 for ap in AIRPORTS_BACKFILL}
    for r in rows:
        ap = r.get("airport")
        if ap in c:
            c[ap] += 1
    return c


def backfill_day(day: date, ya_client: httpx.Client) -> tuple[int, int]:
    """Обработать один день. Возвращает (gates_filled, flights_added)."""
    from src.yandex_departures import supplement_dme

    csv_path = DAILY_DIR / f"{day.isoformat()}.csv"
    if not csv_path.exists():
        log.info("[%s] CSV не найден — пропускаем", day)
        return 0, 0

    rows = _read_csv(csv_path)
    before = _counts(rows)

    # Шаг 1: добрать РЕЙСЫ из снапшота табло по всем аэропортам
    # (кодшеринги склеиваются, ADB-дубли не добавляются — см. daily_fetch).
    snap_added = 0
    for ap in AIRPORTS_BACKFILL:
        snap_added += add_missing_flights_from_snapshot(rows, day, ap)

    # Шаг 2: гейты DME из снапшота для рейсов без гейта
    gates_filled = fill_dme_gates(rows, day)

    # Шаг 3: пропущенные рейсы DME из Яндекс.Расписания (мелкие перевозчики)
    dme_rows = [r for r in rows if r.get("airport") == "DME"]
    try:
        new_rows = supplement_dme(dme_rows, day, client=ya_client)
    except Exception as e:
        log.warning("[%s] Яндекс не отдал данные: %s", day, e)
        new_rows = []
    if new_rows:
        rows.extend(new_rows)

    after = _counts(rows)
    added_total = snap_added + len(new_rows)

    if gates_filled or added_total:
        write_csv(day, rows)
        log.info(
            "[%s] ✓  Гейтов +%d, из снапшота +%d, из Яндекса +%d.  "
            "SVO %d→%d, VKO %d→%d, DME %d→%d",
            day, gates_filled, snap_added, len(new_rows),
            before["SVO"], after["SVO"],
            before["VKO"], after["VKO"],
            before["DME"], after["DME"],
        )
    else:
        log.info("[%s] — без изменений (SVO %d, VKO %d, DME %d)",
                 day, before["SVO"], before["VKO"], before["DME"])

    return gates_filled, added_total


def main() -> int:
    # Определяем диапазон дат
    from_env = os.environ.get("BACKFILL_FROM", "").strip()
    to_env   = os.environ.get("BACKFILL_TO",   "").strip()
    month_env = os.environ.get("BACKFILL_MONTH", "").strip()

    if from_env and to_env:
        start = date.fromisoformat(from_env)
        end   = date.fromisoformat(to_env)
    elif month_env:
        start, end = _month_range(month_env)
    else:
        m = _prev_month()
        log.info("BACKFILL_MONTH не задан → беру прошлый месяц: %s", m)
        start, end = _month_range(m)

    log.info("=== Backfill (SVO/VKO/DME из снапшотов): %s — %s ===", start, end)

    total_gates = total_flights = processed = 0

    headers = {"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )}
    with httpx.Client(timeout=30, headers=headers) as ya_client:
        d = start
        while d <= end:
            if d >= date.today():
                log.info("[%s] Пропускаем: дата ещё не прошла", d)
                d += timedelta(days=1)
                continue

            g, f = backfill_day(d, ya_client)
            total_gates   += g
            total_flights += f
            processed     += 1

            # Пауза только если делали сетевой запрос к Яндексу
            if f >= 0:
                time_module.sleep(_YANDEX_DELAY_SEC)

            d += timedelta(days=1)

    log.info(
        "=== Готово: %d дней обработано, гейтов +%d, рейсов +%d ===",
        processed, total_gates, total_flights,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
