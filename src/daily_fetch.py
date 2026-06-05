"""Ежедневный сбор: ПОЗАВЧЕРАШНИЙ день по SVO/VKO/DME через AeroDataBox -> CSV.

Берём именно позавчера, а не вчера: к этому моменту исторические данные FIDS
по всем аэропортам полностью наполнены (DME дозревает до суток). Это убирает
HTTP 204 «данных пока нет» и пустые фактические времена у ночных рейсов —
без всяких retry, дозаписей и перепроверок. Один CSV на день, три аэропорта.

Можно вручную собрать конкретную дату: задать FETCH_DATE=YYYY-MM-DD
(workflow_dispatch). Тогда берётся она, а не позавчера.

Колонки CSV:
  airport, flight_date, scheduled_time, actual_time, terminal, gate,
  airlines, flight_numbers, destination, destination_iata
"""
from __future__ import annotations

import csv
import os
import sys
import time as time_module
from datetime import date

import httpx

from src.aerodatabox import (
    AIRPORTS, AeroDataBoxError, NoDataYetError, MONTHLY_BUDGET,
    fetch_airport_day, remaining_budget,
)
from src.config import DAILY_DIR, REQUEST_TIMEOUT_SEC
from src.utils import get_logger, day_before_yesterday_msk

log = get_logger("daily_fetch")

CSV_FIELDS = [
    "airport", "flight_date", "scheduled_time", "actual_time",
    "terminal", "gate", "airlines", "flight_numbers",
    "destination", "destination_iata",
]

AIRPORT_RETRIES = 3


def fill_dme_gates(rows: list[dict], day: date) -> int:
    """Для рейсов DME без гейта подставить гейт из снапшота табло Яндекса.
    Сопоставление по (номер рейса, плановое время). Возвращает число
    заполненных. AeroDataBox остаётся источником истины по составу рейсов —
    Яндекс лишь дополняет недостающие гейты."""
    try:
        from src.yandex_board import load_snapshot
    except Exception:
        return 0
    snap = load_snapshot(day)
    if not snap:
        return 0
    # индекс снапшота: по каждому отдельному номеру рейса + время
    by_key = {}
    for v in snap.values():
        t = v.get("time", "")
        for token in str(v.get("flight", "")).replace(",", " ").split():
            pass  # flight в снапшоте — один номер вида "U6 1343"
        by_key[(v.get("flight", ""), t)] = v
    filled = 0
    for r in rows:
        if r["airport"] != "DME":
            continue
        if str(r.get("gate", "")).strip():
            continue
        t = r.get("scheduled_time", "")
        # у рейса может быть несколько номеров (кодшеринг) — пробуем каждый
        for num in str(r.get("flight_numbers", "")).split(","):
            num = num.strip()
            hit = by_key.get((num, t))
            if hit and hit.get("gate"):
                r["gate"] = hit["gate"]
                if not str(r.get("terminal", "")).strip() and hit.get("terminal"):
                    r["terminal"] = hit["terminal"]
                filled += 1
                break
    return filled


def write_csv(day: date, rows: list[dict]) -> str:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


def resolve_target_day() -> date:
    """Целевой день. FETCH_DATE (YYYY-MM-DD) — ручной сбор конкретной даты,
    иначе позавчера по МСК (данные уже устоялись)."""
    raw = os.environ.get("FETCH_DATE", "").strip()
    if raw:
        try:
            y, m, d = (int(x) for x in raw.split("-"))
            chosen = date(y, m, d)
            log.info("FETCH_DATE задан вручную: %s", chosen)
            return chosen
        except (ValueError, TypeError):
            log.warning("FETCH_DATE='%s' не распознан (нужен YYYY-MM-DD), "
                        "беру позавчера", raw)
    return day_before_yesterday_msk()


def main() -> int:
    log.info("=== daily_fetch ВЕРСИЯ 2026-06-04-yagates (гейты DME из табло Яндекса) ===")
    api_key = os.environ.get("AERODATABOX_KEY", "").strip()
    if not api_key:
        log.error("Нет AERODATABOX_KEY в окружении — нечем авторизоваться")
        return 1

    day = resolve_target_day()
    log.info("Сбор за %s. Остаток бюджета: %d/%d",
             day, remaining_budget(), MONTHLY_BUDGET)

    all_rows: list[dict] = []
    failed: list[str] = []

    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as client:
        for i, airport in enumerate(AIRPORTS):
            rows = None
            for att in range(1, AIRPORT_RETRIES + 1):
                try:
                    rows = fetch_airport_day(api_key, airport, day, client)
                    break
                except NoDataYetError as e:
                    # данных нет даже за позавчера — задержка >2 суток (редкость).
                    # Не повторяем; страховочное предупреждение ниже.
                    log.error("[%s] %s", airport, e)
                    break
                except AeroDataBoxError as e:
                    log.error("[%s] попытка %d/%d не удалась: %s",
                              airport, att, AIRPORT_RETRIES, e)
                    if att < AIRPORT_RETRIES:
                        time_module.sleep(10 * att)
            if rows is not None:
                all_rows.extend(rows)
            else:
                failed.append(airport)
            if i < len(AIRPORTS) - 1:
                time_module.sleep(4)

    if not all_rows:
        log.error("Ни одной строки не собрано (ошибки: %s)", failed)
        return 1

    by_airport: dict[str, int] = {}
    for r in all_rows:
        by_airport[r["airport"]] = by_airport.get(r["airport"], 0) + 1

    # СТРАХОВКА: за позавчера данные обязаны быть полными. Если какой-то
    # аэропорт пуст или подозрительно мал — данные дозревают дольше 2 суток
    # (необычно). Громко предупреждаем в лог, чтобы заметить и собрать вручную
    # с большей задержкой. Автоматических перезапросов НЕ делаем.
    MIN_EXPECTED = 30  # ниже этого по аэропорту за сутки — явно неполно
    for airport in AIRPORTS:
        n = by_airport.get(airport, 0)
        if airport in failed or n == 0:
            log.error("СТРАХОВКА: [%s] за %s пусто — данные дозревают дольше "
                      "2 суток? Собери вручную позже (FETCH_DATE=%s).",
                      airport, day, day)
        elif n < MIN_EXPECTED:
            log.warning("СТРАХОВКА: [%s] за %s только %d рейсов — подозрительно "
                        "мало, проверь полноту.", airport, day, n)

    # дополнить недостающие гейты DME из снапшота табло Яндекса
    filled = fill_dme_gates(all_rows, day)
    if filled:
        log.info("Дополнено гейтов DME из табло Яндекса: %d", filled)

    path = write_csv(day, all_rows)
    log.info("Записано %d строк в %s. По аэропортам: %s. Ошибки: %s",
             len(all_rows), path, by_airport, failed or "нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
