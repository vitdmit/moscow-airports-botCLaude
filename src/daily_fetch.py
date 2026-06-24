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

ИЗМЕНЕНИЕ 2026-06 (supplement DME из Яндекс.Расписания):
  AeroDataBox пропускает ~8-9 рейсов/день по DME (мелкие перевозчики,
  не в FIDS-базе). После AeroDataBox-сбора дополняем DME рейсами из
  Яндекс.Расписания (только те, которых нет по номеру рейса). Новые строки
  имеют пустые actual_time и destination_iata, но корректные gate/terminal
  если рейс найден в снапшоте Яндекс-табло.

ИЗМЕНЕНИЕ 2026-06-24 (учёт переноса через дату):
  fill_dme_gates теперь проверяет снапшоты ДНЯ и ДНЯ+1. Если рейс был
  задержан и получил гейт уже после полуночи, гейт окажется в файле D+1.json.
  Объединение снапшотов двух дней позволяет не терять такие гейты.
"""
from __future__ import annotations

import csv
import os
import sys
import time as time_module
from datetime import date, timedelta

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

    Проверяет снапшоты ДНЯ (D) и СЛЕДУЮЩЕГО ДНЯ (D+1).

    Почему D+1: если рейс задержали и он получил гейт уже после полуночи,
    снапшот зафиксировал этот гейт в файле D+1.json (логика _flight_day()
    в yandex_board.py). При обогащении данных за D мы обязаны заглянуть
    и в D+1.json, иначе потеряем такие гейты.

    Сопоставление по (номер рейса, плановое время). Возвращает число заполненных.
    AeroDataBox остаётся источником истины по составу рейсов.
    """
    try:
        from src.yandex_board import load_snapshot
    except Exception:
        return 0

    snap_d  = load_snapshot(day)
    snap_d1 = load_snapshot(day + timedelta(days=1))

    # Объединяем: снапшот дня D приоритетнее D+1 при совпадении ключа
    combined = {**snap_d1, **snap_d}
    if not combined:
        return 0

    # Индекс: (номер рейса, плановое время) → запись снапшота
    by_key: dict[tuple[str, str], dict] = {}
    for v in combined.values():
        t = v.get("time", "")
        by_key[(v.get("flight", ""), t)] = v

    filled = 0
    for r in rows:
        if r["airport"] != "DME":
            continue
        if str(r.get("gate", "")).strip():
            continue
        t = r.get("scheduled_time", "")
        # У кодшеринга несколько номеров — пробуем каждый
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


def supplement_dme_from_yandex(all_rows: list[dict], day: date,
                                client: httpx.Client) -> int:
    """Дополнить DME рейсами из Яндекс.Расписания — теми, которых нет в AeroDataBox.

    Работает только для DME. Для SVO и VKO AeroDataBox покрывает данные полнее.
    При любой ошибке молча возвращает 0 (не роняет основной сбор).
    Возвращает число добавленных строк.
    """
    try:
        from src.yandex_departures import supplement_dme
    except Exception as e:
        log.warning("Не удалось импортировать yandex_departures: %s", e)
        return 0

    try:
        dme_rows = [r for r in all_rows if r["airport"] == "DME"]
        new_rows = supplement_dme(dme_rows, day, client=client)
        if new_rows:
            all_rows.extend(new_rows)
            log.info("[DME] Яндекс дополнил %d рейсов за %s", len(new_rows), day)
        return len(new_rows)
    except Exception as e:
        log.warning("[DME] supplement_dme_from_yandex: неожиданная ошибка: %s", e)
        return 0


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
    log.info("=== daily_fetch 2026-06-24 (гейты D+D+1 + рейсы DME из Яндекса) ===")
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

    MIN_EXPECTED = 30
    for airport in AIRPORTS:
        n = by_airport.get(airport, 0)
        if airport in failed or n == 0:
            log.error("СТРАХОВКА: [%s] за %s пусто — данные дозревают дольше "
                      "2 суток? Собери вручную позже (FETCH_DATE=%s).",
                      airport, day, day)
        elif n < MIN_EXPECTED:
            log.warning("СТРАХОВКА: [%s] за %s только %d рейсов — подозрительно "
                        "мало, проверь полноту.", airport, day, n)

    # Шаг 1: дополнить недостающие гейты DME из снапшота табло Яндекса
    # (проверяет снапшоты D и D+1 для задержанных рейсов)
    filled = fill_dme_gates(all_rows, day)
    if filled:
        log.info("Дополнено гейтов DME из табло Яндекса: %d", filled)

    # Шаг 2: дополнить СОСТАВ рейсов DME из исторического табло Яндекс.Расписания
    # (AeroDataBox пропускает ~8-9 мелких перевозчиков/день по DME)
    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                                             "Chrome/126.0.0.0 Safari/537.36"}) as ya_client:
        added = supplement_dme_from_yandex(all_rows, day, ya_client)

    # Итоговые счётчики
    by_airport_final: dict[str, int] = {}
    for r in all_rows:
        by_airport_final[r["airport"]] = by_airport_final.get(r["airport"], 0) + 1

    path = write_csv(day, all_rows)
    log.info(
        "Записано %d строк в %s. По аэропортам: %s "
        "(DME: %d от ADB + %d от Яндекса). Ошибки: %s",
        len(all_rows), path, by_airport_final,
        by_airport.get("DME", 0), added,
        failed or "нет",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
