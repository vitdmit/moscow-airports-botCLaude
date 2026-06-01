"""Ежедневный сбор: вчерашний день по SVO/VKO/DME через AeroDataBox -> CSV.

Запускается раз в сутки из GitHub Actions (утром, когда вчерашние сутки
полностью закрыты и данные в API устоялись). Один CSV на день со всеми
тремя аэропортами.

Колонки CSV:
  airport, flight_date, scheduled_time, actual_time, terminal, gate,
  airlines, flight_numbers, destination, destination_iata
"""
from __future__ import annotations

import csv
import os
import sys
import time as time_module
from datetime import date, timedelta

import httpx

from src.aerodatabox import (
    AIRPORTS, AeroDataBoxError, MONTHLY_BUDGET,
    fetch_airport_day, remaining_budget,
)
from src.config import DAILY_DIR, REQUEST_TIMEOUT_SEC
from src.utils import get_logger, yesterday_msk

log = get_logger("daily_fetch")

CSV_FIELDS = [
    "airport", "flight_date", "scheduled_time", "actual_time",
    "terminal", "gate", "airlines", "flight_numbers",
    "destination", "destination_iata",
]


def write_csv(day: date, rows: list[dict]) -> str:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


def main() -> int:
    api_key = os.environ.get("AERODATABOX_KEY", "").strip()
    if not api_key:
        log.error("Нет AERODATABOX_KEY в окружении — нечем авторизоваться")
        return 1

    day = yesterday_msk()
    log.info("Сбор за %s. Остаток бюджета: %d/%d",
             day, remaining_budget(), MONTHLY_BUDGET)

    all_rows: list[dict] = []
    failed: list[str] = []

    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as client:
        for i, airport in enumerate(AIRPORTS):
            try:
                rows = fetch_airport_day(api_key, airport, day, client)
                all_rows.extend(rows)
            except AeroDataBoxError as e:
                log.error("[%s] не удалось забрать день: %s", airport, e)
                failed.append(airport)
            if i < len(AIRPORTS) - 1:
                time_module.sleep(4)  # вежливая пауза между аэропортами

    if not all_rows:
        log.error("Ни одной строки не собрано (аэропорты с ошибкой: %s)", failed)
        return 1

    path = write_csv(day, all_rows)
    by_airport: dict[str, int] = {}
    for r in all_rows:
        by_airport[r["airport"]] = by_airport.get(r["airport"], 0) + 1
    log.info("Записано %d строк в %s. По аэропортам: %s. Ошибки: %s",
             len(all_rows), path, by_airport, failed or "нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
