"""Диагностика: что AeroDataBox реально отдаёт по аэропорту за дату.

Ничего не пишет в data/daily. Только печатает. Нужен, чтобы проверить
гипотезу: часть рейсов теряется не в API, а в белом списке
DEPARTED_STATUSES из src/aerodatabox.py (всё, что не departed/enroute/
arrived, выпадает молча).

Запуск:
    DIAG_DATES=2026-08-26,2026-08-27 DIAG_AIRPORT=DME python -m src.diag_statuses
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import date, datetime, timedelta

import httpx

from src.aerodatabox import (DEPARTED_STATUSES, EXCLUDED_STATUSES, _bump_usage,
                             fetch_window)


def _sched_local(item: dict) -> str:
    dep = item.get("movement") or item.get("departure") or {}
    return (dep.get("scheduledTime") or {}).get("local") or ""


def _hour(item: dict) -> str:
    s = _sched_local(item)
    return s[11:13] if len(s) >= 13 else "??"


def _dest(item: dict) -> str:
    dep = item.get("movement") or item.get("departure") or {}
    ap = dep.get("airport") or (item.get("arrival") or {}).get("airport") or {}
    return ap.get("name") or ap.get("iata") or "?"


def _gate(item: dict) -> str:
    dep = item.get("movement") or item.get("departure") or {}
    return str(dep.get("gate") or "нет")


def main() -> int:
    key = os.environ.get("AERODATABOX_KEY") or ""
    if not key:
        print("нет AERODATABOX_KEY в окружении")
        return 1
    airport = (os.environ.get("DIAG_AIRPORT") or "DME").strip().upper()
    dates = [d.strip() for d in (os.environ.get("DIAG_DATES") or "").split(",") if d.strip()]
    if not dates:
        print("не заданы даты в DIAG_DATES")
        return 1

    with httpx.Client(timeout=60) as client:
        for ds in dates:
            day = date.fromisoformat(ds)
            d0 = datetime(day.year, day.month, day.day, 0, 0)
            windows = [(d0, d0 + timedelta(hours=12)),
                       (d0 + timedelta(hours=12), d0 + timedelta(hours=24))]
            items: list[dict] = []
            for f, t in windows:
                got: list[dict] = []
                try:
                    payload = fetch_window(key, airport, f, t, client=client)
                    got = payload.get("departures") or []
                except Exception as exc:
                    print("[%s] %s окно %s-%s: %s %s"
                          % (airport, ds, f.strftime("%H:%M"), t.strftime("%H:%M"),
                             type(exc).__name__, exc))
                _bump_usage()
                print("[%s] %s окно %s-%s: рейсов в сыром ответе %d"
                      % (airport, ds, f.strftime("%H:%M"), t.strftime("%H:%M"), len(got)))
                items.extend(got)

            statuses: Counter = Counter()
            lost_hours: Counter = Counter()
            passed = excluded = silent = cargo = 0
            examples: list[tuple] = []
            for it in items:
                if it.get("isCargo"):
                    cargo += 1
                    continue
                st = (it.get("status") or "").strip()
                statuses[st or "(пусто)"] += 1
                low = st.lower()
                if low in EXCLUDED_STATUSES:
                    excluded += 1
                elif low in DEPARTED_STATUSES:
                    passed += 1
                else:
                    silent += 1
                    lost_hours[_hour(it)] += 1
                    if len(examples) < 15:
                        examples.append((_sched_local(it)[11:16], _dest(it),
                                         (it.get("number") or "?").strip(),
                                         _gate(it), st or "(пусто)"))

            print("")
            print("=== %s %s ===" % (airport, ds))
            print("всего в ответе: %d, из них грузовых: %d" % (len(items), cargo))
            print("проходит белый список departed/enroute/arrived: %d" % passed)
            print("режет чёрный список (отмена, диверсия): %d" % excluded)
            print("ТЕРЯЕТСЯ МОЛЧА (не в одном и не в другом списке): %d" % silent)
            print("статусы: " + ", ".join("%s=%d" % (k, v) for k, v in statuses.most_common()))
            if lost_hours:
                print("потерянные по часам: "
                      + ", ".join("%s=%d" % (h, n) for h, n in sorted(lost_hours.items())))
                print("примеры потерянных (время, направление, рейс, гейт, статус):")
                for e in examples:
                    print("   " + " | ".join(str(x) for x in e))
            print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
