"""Прилёты по SVO/VKO/DME за сутки из табло Яндекс Расписаний (с 16.11.2026).

Источник тот же, что у вылетов: страница станции с event=arrival, снимок
data/yandex_raw/<дата>/<аэропорт>_arr.json.gz (снимает облачная задача
вместе с вылетами, см. src/yandex_snapshot.py). Архив Яндекса открыт на
30 дней, поэтому прилёты есть с 17.10.2026.

Правила те же, что у вылетов (src/yandex_fids.py):
  - прилетевший рейс относится к дате ФАКТИЧЕСКОГО прилёта;
  - кодшеринги склеиваются в один борт;
  - статус unknown и бизнес-терминалы (SVO A, VKO 3) не учитываются;
  - отменённые в файл рейсов не пишутся.

Зона ВВЛ/МВЛ: в Шереметьеве по терминалу (B и D внутренние, C
международный, как и у вылетов: src.zones.zone_by_place). Во Внукове и
Домодедове терминала прилёта на табло нет или он общий, зона берётся по
аэропорту вылета (код IATA, запасной путь по названию города).

Выход:
  data/arrivals/<дата>.csv          один прилетевший борт = одна строка
  data/arrivals/<дата>.summary.csv  число рейсов: аэропорт, зона, терминал,
                                    город вылета

Запуск: python -m src.arrivals [YYYY-MM-DD ...]  (без даты = позавчера МСК)
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import date

from src import yandex_fids as yf
from src.config import AIRPORTS, DATA_DIR
from src.utils import day_before_yesterday_msk, get_logger
from src.zones import zone, zone_by_iata, zone_by_place

log = get_logger("arrivals")

ARR_DIR = DATA_DIR / "arrivals"

CSV_FIELDS = [
    "airport", "flight_date", "scheduled_time", "actual_time", "terminal",
    "zone", "airlines", "flight_numbers", "origin", "origin_iata",
]
SUMMARY_FIELDS = ["airport", "zone", "terminal", "origin", "origin_iata", "flights"]

# Терминал в сводке нужен только там, где он делит зону на части:
# в Шереметьеве ВВЛ это B и D, МВЛ это C. Во Внукове один терминал A,
# в Домодедове терминал прилёта на табло не указан.
TERMINAL_IN_SUMMARY = {"SVO"}


def arrival_zone(airport: str, terminal: str, origin: str, origin_iata: str) -> str:
    z = zone_by_place(airport, terminal, "") if airport == "SVO" else None
    if z:
        return z
    z = zone_by_iata(origin_iata)
    if z:
        return z
    z = zone(origin)
    return z if z in ("ВВЛ", "МВЛ") else "?"


def rows_for_day(airport: str, day: date) -> list[dict]:
    station = yf.fetch_station(airport, day, event="arrival")
    flights = yf.flights_for_day(airport, station, day)
    if len(flights) < yf.MIN_FLIGHTS:
        raise yf.YandexError("[%s] %s: прилётов всего %d, страница неполная"
                             % (airport, day, len(flights)))
    rows = []
    for f in flights:
        if f["status"] == "cancelled":
            continue
        rows.append({
            "airport": airport,
            "flight_date": day.isoformat(),
            "scheduled_time": f["sched"].strftime("%H:%M"),
            "actual_time": f["actual"].strftime("%H:%M") if f["actual"] else "",
            "terminal": f["terminal"],
            "zone": arrival_zone(airport, f["terminal"], f["dest"], f["dest_iata"]),
            "airlines": ",".join(f["airlines"]),
            "flight_numbers": ",".join(f["numbers"]),
            "origin": f["dest"],
            "origin_iata": f["dest_iata"],
        })
    return rows


def summary(rows: list[dict]) -> list[dict]:
    cnt = Counter()
    for r in rows:
        term = r["terminal"] if r["airport"] in TERMINAL_IN_SUMMARY else ""
        cnt[(r["airport"], r["zone"], term, r["origin"], r["origin_iata"])] += 1
    order = {ap: i for i, ap in enumerate(AIRPORTS)}
    out = [dict(zip(SUMMARY_FIELDS, k + (n,))) for k, n in cnt.items()]
    out.sort(key=lambda r: (order.get(r["airport"], 9), r["zone"], r["terminal"],
                            -r["flights"], r["origin"]))
    return out


def _write(path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def build_day(day: date) -> bool:
    """Собрать сутки. False, если хотя бы по одному аэропорту снимка нет:
    тогда файлы не пишутся, чтобы не оставить неполные сутки."""
    rows = []
    for ap in AIRPORTS:
        try:
            rows += rows_for_day(ap, day)
        except yf.YandexError as e:
            log.error("%s", e)
            return False
    _write(ARR_DIR / ("%s.csv" % day.isoformat()), CSV_FIELDS, rows)
    _write(ARR_DIR / ("%s.summary.csv" % day.isoformat()), SUMMARY_FIELDS, summary(rows))
    by = Counter((r["airport"], r["zone"]) for r in rows)
    log.info("%s: прилётов %d (%s)", day, len(rows),
             ", ".join("%s %s %d" % (a, z, n) for (a, z), n in sorted(by.items())))
    return True


def main(argv: list[str]) -> int:
    days = [date.fromisoformat(a) for a in argv] or [day_before_yesterday_msk()]
    bad = [d for d in days if not build_day(d)]
    for d in bad:
        print("ARRIVALS FAIL %s" % d)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
