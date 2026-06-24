"""Ретроактивное дополнение DME-данных за месяц.

Обновляет существующие CSV в data/daily/ двумя способами:
  1. Обогащение гейтами из имеющихся gate_snapshots (без сетевых запросов)
  2. Добавление пропущенных рейсов из исторического Яндекс.Расписания (бесплатно)

НОЛЬ затрат бюджета AeroDataBox. Перезаписывает CSV только если что-то изменилось.

Параметры (env-переменные или GitHub Actions inputs):
  BACKFILL_MONTH   YYYY-MM     месяц для обработки (по умолчанию: прошлый)
  BACKFILL_FROM    YYYY-MM-DD  начать с этой даты (приоритет над MONTH)
  BACKFILL_TO      YYYY-MM-DD  закончить этой датой (используется вместе с FROM)

Запуск:
  Через GitHub Actions: Actions → «Бэкфилл DME» → Run workflow
  Вручную из корня репо: python scripts/backfill_dme.py

Пример ручного запуска за конкретный диапазон:
  BACKFILL_FROM=2026-06-01 BACKFILL_TO=2026-06-21 python scripts/backfill_dme.py
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
from src.daily_fetch import fill_dme_gates, write_csv
from src.utils import get_logger

log = get_logger("backfill_dme")

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


def backfill_day(day: date, ya_client: httpx.Client) -> tuple[int, int]:
    """Обработать один день. Возвращает (gates_filled, flights_added)."""
    from src.yandex_departures import supplement_dme

    csv_path = DAILY_DIR / f"{day.isoformat()}.csv"
    if not csv_path.exists():
        log.info("[%s] CSV не найден — пропускаем", day)
        return 0, 0

    rows = _read_csv(csv_path)
    dme_before = sum(1 for r in rows if r.get("airport") == "DME")
    if dme_before == 0:
        log.warning("[%s] Нет строк DME в CSV — пропускаем", day)
        return 0, 0

    # Шаг 1: гейты из снапшота (только файловые операции, сеть не нужна)
    gates_filled = fill_dme_gates(rows, day)

    # Шаг 2: пропущенные рейсы из Яндекс.Расписания
    dme_rows = [r for r in rows if r.get("airport") == "DME"]
    try:
        new_rows = supplement_dme(dme_rows, day, client=ya_client)
    except Exception as e:
        log.warning("[%s] Яндекс не отдал данные: %s", day, e)
        new_rows = []

    if new_rows:
        rows.extend(new_rows)

    dme_after = sum(1 for r in rows if r.get("airport") == "DME")

    if gates_filled or new_rows:
        write_csv(day, rows)
        log.info(
            "[%s] ✓  Гейтов: +%d  Рейсов: +%d  DME: %d → %d",
            day, gates_filled, len(new_rows), dme_before, dme_after,
        )
    else:
        log.info("[%s] — без изменений (DME: %d)", day, dme_before)

    return gates_filled, len(new_rows)


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

    log.info("=== Backfill DME: %s — %s ===", start, end)

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
