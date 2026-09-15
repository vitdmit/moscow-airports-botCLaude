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
            "destination": "",
            "flights": [],
        })
        if not grp["destination"] and str(v.get("destination", "")).strip():
            grp["destination"] = str(v.get("destination", "")).strip()
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
            # Направление с доски (рус.) — запасной вариант, если по истории
            # найти маршрут не удастся. Не пишется в CSV (extrasaction=ignore).
            "_snap_dest": grp.get("destination", ""),
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


def enrich_from_history(rows: list[dict]) -> int:
    """Заполнить пустые airlines/destination/destination_iata у строк, добранных
    из снапшота (в снапшоте лежат только рейс/время/гейт/терминал).

    Источники восстановления:
      - направление и IATA: по номеру рейса из уже собранных дней в DAILY_DIR
        (тот же рейс — тот же маршрут);
      - авиакомпания: по префиксу номера (код перевозчика: S7, U6, YC, HH…),
        имя берём наиболее частое из истории.

    Ничего не перезаписывает, если поле уже заполнено. Возвращает число
    заполненных направлений.
    """
    import glob as _glob

    dest: dict[str, str] = {}
    iata: dict[str, str] = {}
    pref_air: dict[str, dict[str, int]] = {}
    for p in _glob.glob(str(DAILY_DIR / "*.csv")):
        try:
            with open(p, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    for num in str(r.get("flight_numbers", "")).split(","):
                        k = _norm_num(num)
                        if not k:
                            continue
                        d = str(r.get("destination", "")).strip()
                        if d and k not in dest:
                            dest[k] = d
                            iata[k] = str(r.get("destination_iata", "")).strip()
                        a = str(r.get("airlines", "")).strip()
                        m = re.match(r"^[A-Z0-9]+", k)
                        if a and m:
                            pa = pref_air.setdefault(m.group(0), {})
                            pa[a] = pa.get(a, 0) + 1
        except Exception:
            continue

    air = {p: max(v, key=v.get) for p, v in pref_air.items()}

    filled = 0
    for r in rows:
        first = _norm_num(str(r.get("flight_numbers", "")).split(",")[0])
        if not first:
            continue
        if not str(r.get("destination", "")).strip() and first in dest:
            r["destination"] = dest[first]
            if not str(r.get("destination_iata", "")).strip():
                r["destination_iata"] = iata.get(first, "")
            filled += 1
        if not str(r.get("airlines", "")).strip():
            m = re.match(r"^[A-Z0-9]+", first)
            if m and m.group(0) in air:
                r["airlines"] = air[m.group(0)]
        # запасной вариант: направление с доски (рус.), если в истории не нашли
        if not str(r.get("destination", "")).strip() \
                and str(r.get("_snap_dest", "")).strip():
            r["destination"] = str(r.get("_snap_dest", "")).strip()
    if filled:
        log.info("Восстановлено направлений из истории DAILY_DIR: %d", filled)
    return filled


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


# Порядок аэропортов в итоговом CSV — как в AIRPORTS (SVO, VKO, DME)
_AIRPORT_ORDER = {ap: i for i, ap in enumerate(AIRPORTS)}


def _sort_key(r: dict) -> tuple:
    """Ключ сортировки строк CSV: аэропорт (SVO→VKO→DME), время, терминал, гейт.

    ИСПРАВЛЕНИЕ 2026-07-11: строки, добавленные ПОСЛЕ основного сбора
    (Яндекс-дополнение DME, само-лечение из снапшотов), раньше просто
    дописывались в конец файла — внизу возникал второй блок SVO/VKO/DME
    вперемешку. Теперь перед записью весь файл сортируется единообразно.
    Ни одна строка при сортировке не теряется и не меняется.
    """
    return (
        _AIRPORT_ORDER.get(str(r.get("airport", "")), 99),
        str(r.get("scheduled_time", "")),
        str(r.get("terminal", "")),
        str(r.get("gate", "")),
    )


def write_csv(day: date, rows: list[dict]) -> str:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=_sort_key):
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


def _existing_rows(day: date, airports: list[str]) -> list[dict]:
    """Строки уже записанного CSV за день по указанным аэропортам."""
    path = DAILY_DIR / f"{day.isoformat()}.csv"
    if not path.exists() or not airports:
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("airport") in airports]


def _fetch_adb(airports: list[str], day: date) -> tuple[list[dict], list[str]]:
    """Запасной путь: сутки из AeroDataBox (прежний сбор без изменений)."""
    api_key = os.environ.get("AERODATABOX_KEY", "").strip()
    if not api_key:
        log.error("Нет AERODATABOX_KEY, запасной источник недоступен для %s", airports)
        return [], list(airports)
    log.info("AeroDataBox за %s по %s. Остаток бюджета: %d/%d",
             day, airports, remaining_budget(), MONTHLY_BUDGET)
    rows_all: list[dict] = []
    failed: list[str] = []
    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as client:
        for i, airport in enumerate(airports):
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
            if rows:
                rows_all.extend(rows)
            else:
                failed.append(airport)
            if i < len(airports) - 1:
                time_module.sleep(4)
    got = [a for a in airports if a not in failed]
    if "DME" in got:
        filled = fill_dme_gates(rows_all, day)
        if filled:
            log.info("Дополнено гейтов DME из снапшотов: %d", filled)
    for ap in got:
        n = add_missing_flights_from_snapshot(rows_all, day, ap)
        if n:
            log.info("[%s] добрано из снапшотов табло: %d", ap, n)
    enrich_from_history(rows_all)
    return rows_all, failed


def main() -> int:
    """Сбор суток.

    С 15.09.2026 основа табло Яндекс Расписаний (src/yandex_fids.py): план,
    отмены, факт, гейт. К нему дописываются рейсы AeroDataBox, которых на
    табло нет (часть перевозчиков Яндекс не показывает). Если Яндекс сутки
    не отдал, аэропорт берётся из AeroDataBox целиком. Если не отдал никто,
    прежние строки аэропорта в CSV сохраняются."""
    from src import yandex_fids

    day = resolve_target_day()
    log.info("=== daily_fetch 2026-09: сбор за %s, Яндекс + AeroDataBox ===", day)

    yandex: dict[str, list[dict]] = {}
    with yandex_fids.make_client() as yc:
        for airport in AIRPORTS:
            try:
                yandex[airport] = yandex_fids.fetch_day(airport, day, yc)
            except yandex_fids.YandexError as e:
                log.error("[%s] Яндекс не отдал сутки %s: %s", airport, day, e)

    adb_rows, adb_failed = _fetch_adb(list(AIRPORTS), day)

    # Сутки, уже собранные по Яндексу, не заменяем одним AeroDataBox, если в
    # этот раз Яндекс не ответил (капча): Яндекс полнее, AeroDataBox по DME
    # теряет целые блоки часов.
    prev_sources: dict = {}
    try:
        from src.ops_report import OPS_DIR
        import json as _json
        pf = OPS_DIR / ("%s.json" % day.isoformat())
        if pf.exists():
            prev_sources = _json.loads(pf.read_text(encoding="utf-8")).get("sources") or {}
    except Exception as exc:
        log.warning("не прочитал источники прежней сводки: %s", exc)

    all_rows: list[dict] = []
    prepared: dict[str, list[dict]] = {}
    source: dict[str, str] = {}
    failed: list[str] = []
    for ap in AIRPORTS:
        ap_adb = [r for r in adb_rows if r.get("airport") == ap]
        if ap not in yandex and "yandex" in str(prev_sources.get(ap, "")):
            log.warning("[%s] %s уже собраны по Яндексу (%s), оставляю их",
                        ap, day, prev_sources[ap])
            failed.append(ap)
            continue
        if ap in yandex:
            rows = yandex_fids.csv_rows(ap, day, yandex[ap])
            # Соседние сутки для защиты от задвоения не смотрим: ежедневный
            # рейс, которого нет у Яндекса (R8 484), иначе терялся бы через день.
            extra = yandex_fids.adb_extra(ap, ap_adb)
            all_rows.extend(rows)
            all_rows.extend(extra)
            prepared[ap] = (yandex_fids.ops_flights(yandex[ap])
                            + yandex_fids.ops_from_csv(extra))
            source[ap] = "yandex %d + aerodatabox %d" % (len(rows), len(extra))
            if extra:
                log.info("[%s] %s: из AeroDataBox дописано %d рейсов, которых нет у Яндекса: %s",
                         ap, day, len(extra),
                         ", ".join(r.get("flight_numbers", "") for r in extra))
        elif ap_adb:
            all_rows.extend(ap_adb)
            source[ap] = "aerodatabox %d" % len(ap_adb)
        else:
            failed.append(ap)

    if failed:
        kept = _existing_rows(day, failed)
        all_rows.extend(kept)
        for ap in failed:
            source[ap] = "прежние строки (%d)" % sum(1 for r in kept if r["airport"] == ap)
        log.warning("За %s оставлены прежние строки по %s", day, failed)

    if not all_rows:
        log.error("Ни одной строки за %s", day)
        return 1

    counts: dict[str, int] = {}
    for r in all_rows:
        counts[r["airport"]] = counts.get(r["airport"], 0) + 1
    path = write_csv(day, all_rows)
    log.info("Записано %d строк в %s. По аэропортам: %s. Источники: %s",
             len(all_rows), path, counts, source)

    try:
        from src.ops_report import build_ops_day
        build_ops_day(day, airports=[a for a in AIRPORTS if a not in failed],
                      prepared=prepared, keep_airports=tuple(failed))
    except Exception as exc:
        log.error("Операционная сводка не собралась: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
