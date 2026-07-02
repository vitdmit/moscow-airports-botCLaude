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

ИЗМЕНЕНИЕ 2026-06-24 (fallback-матчинг гейтов по номеру рейса):
  fill_dme_gates теперь двухступенчатый. Если точный матч (рейс, время)
  не нашёл гейт — ищем только по номеру рейса среди всех записей снапшота.
  Выбирается запись ближайшая по времени к scheduled_time. Это закрывает
  задержанные рейсы, у которых плановое время в ADB и табло расходится.

ИЗМЕНЕНИЕ 2026-07 (само-лечение из снапшотов, все 3 аэропорта):
  add_missing_flights_from_snapshot дополняет СОСТАВ рейсов из снапшота
  живого табло по каждому аэропорту — страховка на случай, когда AeroDataBox
  отдал день частично или пропустил аэропорт целиком (напр. при исчерпании
  месячного бюджета). Кодшеринги (несколько номеров одного физического рейса)
  склеиваются в ОДНУ строку по (время+терминал+гейт), а рейс не добавляется,
  если этот гейт у ADB уже занят примерно в то же время (±20 мин) — так вылет
  не задваивается. Бюджет AeroDataBox не тратится.
"""
from __future__ import annotations

import csv
import os
import re
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

# Допуск (мин): один и тот же гейт не может занимать два РАЗНЫХ физических
# рейса в пределах этого окна — используется, чтобы не добавить кодшеринг,
# который ADB уже записал под другим номером.
GATE_TIME_TOL_MIN = 20


def _norm_num(s: str) -> str:
    """Нормализовать номер рейса для сравнения: 'S7  67' -> 'S767'."""
    return re.sub(r"\s+", "", str(s).strip().upper())


def _time_to_min(t: str) -> int | None:
    """'14:35' -> 875. Некорректное время -> None."""
    try:
        h, m = str(t).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, IndexError, AttributeError):
        return None


def add_missing_flights_from_snapshot(rows: list[dict], day: date,
                                      airport: str) -> int:
    """Добрать рейсы `airport` из снапшота живого табло, которых нет в rows.

    Зачем: если AeroDataBox отдал день частично (или пропустил аэропорт
    целиком — напр. при исчерпании бюджета), недостающие рейсы часто уже
    лежат в накопленном снапшоте табло. Сеть и бюджет AeroDataBox не нужны.

    Кодшеринги: один физический рейс попадает в снапшот несколькими записями
    (разные номера одного рейса). Склеиваем их в ОДНУ строку по ключу
    (время, терминал, гейт) — так вылет не задваивается.

    Защита от задвоения с ADB: рейс не добавляется, если
      - любой из его номеров уже есть среди собранных, ИЛИ
      - тот же гейт у уже собранного рейса занят в пределах ±GATE_TIME_TOL_MIN
        (это ловит кодшеринг, записанный ADB под другим номером).

    Новые строки получают пустые actual_time/destination/destination_iata
    (как и Яндекс-дополнения) — для подсчёта загрузки гейтов этого достаточно.
    Возвращает число добавленных строк (физических рейсов).

    Ограничение: в снапшот попадают только рейсы, которым на табло объявили
    «Выход на посадку». Рейс без публично присвоенного гейта не восстановится.
    """
    try:
        from src.yandex_board import load_snapshot
    except Exception:
        return 0

    snap = load_snapshot(day, airport)
    if not snap:
        return 0

    # Что уже собрано по этому аэропорту: номера и занятые (гейт, минута)
    have_nums: set[str] = set()
    have_gate_time: list[tuple[str, int]] = []
    for r in rows:
        if r.get("airport") != airport:
            continue
        for num in str(r.get("flight_numbers", "")).split(","):
            n = _norm_num(num)
            if n:
                have_nums.add(n)
        g = str(r.get("gate", "")).strip()
        tm = _time_to_min(r.get("scheduled_time", ""))
        if g and tm is not None:
            have_gate_time.append((g, tm))

    # Группируем записи снапшота по (время, терминал, гейт) = один физ. рейс
    groups: dict[tuple[str, str, str], dict] = {}
    for v in snap.values():
        flight = v.get("flight", "")
        if not flight:
            continue
        key = (v.get("time", ""), v.get("terminal", ""), v.get("gate", ""))
        grp = groups.setdefault(key, {
            "time": v.get("time", ""),
            "terminal": v.get("terminal", ""),
            "gate": v.get("gate", ""),
            "flights": [],
        })
        if _norm_num(flight) not in {_norm_num(x) for x in grp["flights"]}:
            grp["flights"].append(flight)

    added = 0
    for grp in groups.values():
        # уже есть по номеру рейса?
        if any(_norm_num(fl) in have_nums for fl in grp["flights"]):
            continue
        # тот же гейт уже занят примерно в то же время (кодшеринг под др. номером)?
        gate = str(grp["gate"]).strip()
        gm = _time_to_min(grp["time"])
        if gate and gm is not None and any(
            g == gate and abs(t - gm) <= GATE_TIME_TOL_MIN
            for g, t in have_gate_time
        ):
            continue

        rows.append({
            "airport": airport,
            "flight_date": day.isoformat(),
            "scheduled_time": grp["time"],
            "actual_time": "",
            "terminal": grp["terminal"],
            "gate": grp["gate"],
            "airlines": "",
            "flight_numbers": ",".join(grp["flights"]),
            "destination": "",
            "destination_iata": "",
        })
        for fl in grp["flights"]:
            have_nums.add(_norm_num(fl))
        if gate and gm is not None:
            have_gate_time.append((gate, gm))
        added += 1

    if added:
        log.info("[%s] Из снапшота табло добрано рейсов %s: %d",
                 day, airport, added)
    return added


def fill_dme_gates(rows: list[dict], day: date) -> int:
    """Для рейсов DME без гейта подставить гейт из снапшота табло Яндекса.

    Проверяет снапшоты ДНЯ (D) и СЛЕДУЮЩЕГО ДНЯ (D+1).

    Почему D+1: если рейс задержали и он получил гейт уже после полуночи,
    снапшот зафиксировал этот гейт в файле D+1.json (логика _flight_day()
    в yandex_board.py). При обогащении данных за D мы обязаны заглянуть
    и в D+1.json, иначе потеряем такие гейты.

    Сопоставление двухступенчатое:
      1. Точное: (номер рейса, плановое время) — основной путь.
      2. Fallback по номеру рейса без времени — для задержанных рейсов,
         у которых scheduled_time в ADB не совпадает с временем в снапшоте.
         Из кандидатов выбирается ближайший по времени к scheduled_time.

    Возвращает число заполненных.
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

    # Индекс 1: точный — (номер рейса, время) → запись снапшота
    by_key: dict[tuple[str, str], dict] = {}
    # Индекс 2: fallback — номер рейса → все записи с этим номером
    by_flight: dict[str, list[dict]] = {}
    for v in combined.values():
        flight = v.get("flight", "")
        t = v.get("time", "")
        by_key[(flight, t)] = v
        if flight:
            by_flight.setdefault(flight, []).append(v)

    def _time_diff_min(snap_time: str, sched_time: str) -> int:
        """Разница в минутах между временем снапшота и плановым временем рейса."""
        if not snap_time or not sched_time:
            return 9999
        try:
            sh, sm = int(snap_time.split(":")[0]), int(snap_time.split(":")[1])
            th, tm = int(sched_time.split(":")[0]), int(sched_time.split(":")[1])
            return abs((sh * 60 + sm) - (th * 60 + tm))
        except Exception:
            return 9999

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
            if not num:
                continue

            # Приоритет 1: точное совпадение (рейс, плановое время)
            hit = by_key.get((num, t))
            if hit and hit.get("gate"):
                r["gate"] = hit["gate"]
                if not str(r.get("terminal", "")).strip() and hit.get("terminal"):
                    r["terminal"] = hit["terminal"]
                filled += 1
                break

            # Приоритет 2: fallback — только по номеру рейса
            # Срабатывает когда рейс задержан и его время в снапшоте отличается
            # от scheduled_time. Берём запись с гейтом, ближайшую по времени.
            candidates = [c for c in by_flight.get(num, []) if c.get("gate")]
            if candidates:
                best = min(candidates,
                           key=lambda c: _time_diff_min(c.get("time", ""), t))
                r["gate"] = best["gate"]
                if not str(r.get("terminal", "")).strip() and best.get("terminal"):
                    r["terminal"] = best["terminal"]
                filled += 1
                log.debug(
                    "[DME] fallback-гейт %s: sched %s → snap %s, гейт %s",
                    num, t, best.get("time"), best.get("gate"),
                )
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
    log.info("=== daily_fetch 2026-07 (гейты D+D+1 + рейсы DME из Яндекса "
             "+ само-лечение из снапшотов по 3 аэропортам) ===")
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

    # Шаг 3: САМО-ЛЕЧЕНИЕ — добрать рейсы из снапшота живого табло по всем
    # аэропортам (страховка на частичный/пропущенный день; кодшеринги склеиваются).
    snap_added: dict[str, int] = {}
    for ap in AIRPORTS:
        n = add_missing_flights_from_snapshot(all_rows, day, ap)
        if n:
            snap_added[ap] = n
    if snap_added:
        log.info("Добрано из снапшотов табло (само-лечение): %s", snap_added)

    # Итоговые счётчики
    by_airport_final: dict[str, int] = {}
    for r in all_rows:
        by_airport_final[r["airport"]] = by_airport_final.get(r["airport"], 0) + 1

    path = write_csv(day, all_rows)
    log.info(
        "Записано %d строк в %s. По аэропортам: %s "
        "(DME: %d от ADB + %d от Яндекса + снапшоты %s). Ошибки: %s",
        len(all_rows), path, by_airport_final,
        by_airport.get("DME", 0), added, snap_added or "0",
        failed or "нет",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
