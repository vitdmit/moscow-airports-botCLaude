"""Диагностика: что именно AeroDataBox называет фактическим временем.

Зачем. Три аэропорта отдают «факт» по-разному. У Шереметьева пик ровно на
плановом времени и провал сразу после него, у Внукова пика нет вовсе, у
Домодедова время округлено до пяти минут чаще, чем бывает при поминутном
замере. Значит в поле revisedTime лежат разные события, и общий процент
задержек по трём аэропортам сравнивать нельзя.

Что делает. За одни сутки по каждому аэропорту выгружает по каждому рейсу
все поля времени, метки качества и статус, и складывает это в
data/diag/times_<дата>.csv. Дальше файл разбирается в DuckDB.

В data/daily ничего не пишет. Расход: 6 запросов (2 окна на аэропорт).

Запуск:
    DIAG_DATE=2026-09-01 python -m src.diag_times
"""
from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import date, datetime, timedelta

import httpx

from src.aerodatabox import AIRPORTS, _bump_usage, fetch_window
from src.config import DATA_DIR

OUT_DIR = DATA_DIR / "diag"

TIME_FIELDS = ("scheduledTime", "revisedTime", "runwayTime", "predictedTime",
               "actualTime", "estimatedTime")


def _local(dep: dict, field: str) -> str:
    v = dep.get(field)
    if isinstance(v, dict):
        return (v.get("local") or "").strip()
    if isinstance(v, str):
        return v.strip()
    return ""


def _quality(dep: dict, item: dict) -> str:
    q = dep.get("quality") or item.get("quality") or []
    if isinstance(q, list):
        return "+".join(str(x) for x in q)
    return str(q)


def collect(key: str, airport: str, day: date, client: httpx.Client) -> list[dict]:
    d0 = datetime(day.year, day.month, day.day, 0, 0)
    windows = [(d0, d0 + timedelta(hours=12)), (d0 + timedelta(hours=12), d0 + timedelta(hours=24))]
    rows: list[dict] = []
    for f, t in windows:
        items = []
        try:
            items = (fetch_window(key, airport, f, t, client=client).get("departures") or [])
        except Exception as exc:
            print("[%s] окно %s: %s %s" % (airport, f.strftime("%H:%M"), type(exc).__name__, exc))
        _bump_usage()
        print("[%s] окно %s-%s: рейсов %d"
              % (airport, f.strftime("%H:%M"), t.strftime("%H:%M"), len(items)))
        for it in items:
            dep = it.get("movement") or it.get("departure") or {}
            row = {"airport": airport, "day": day.isoformat(),
                   "number": (it.get("number") or "").strip(),
                   "status": (it.get("status") or "").strip(),
                   "quality": _quality(dep, it),
                   "is_cargo": "1" if it.get("isCargo") else "0",
                   "terminal": str(dep.get("terminal") or ""),
                   "gate": str(dep.get("gate") or "")}
            for fld in TIME_FIELDS:
                row[fld] = _local(dep, fld)
            rows.append(row)
    return rows


def main() -> int:
    key = os.environ.get("AERODATABOX_KEY") or ""
    if not key:
        print("нет AERODATABOX_KEY в окружении")
        return 1
    ds = (os.environ.get("DIAG_DATE") or "").strip()
    if not ds:
        print("не задана DIAG_DATE")
        return 1
    day = date.fromisoformat(ds)

    rows: list[dict] = []
    with httpx.Client(timeout=60) as client:
        for ap in AIRPORTS:
            rows.extend(collect(key, ap, day, client))

    if not rows:
        print("ничего не пришло")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / ("times_%s.csv" % day.isoformat())
    cols = ["airport", "day", "number", "status", "quality", "is_cargo",
            "terminal", "gate"] + list(TIME_FIELDS)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print("записано %d строк в %s" % (len(rows), path))

    # Короткая сводка прямо в лог, чтобы было видно без выкачивания файла.
    for ap in AIRPORTS:
        sub = [r for r in rows if r["airport"] == ap and r["is_cargo"] == "0"]
        if not sub:
            continue
        print("")
        print("=== %s, рейсов без грузовых: %d" % (ap, len(sub)))
        for fld in TIME_FIELDS:
            n = sum(1 for r in sub if r[fld])
            if n:
                print("   %-15s заполнено у %d (%.0f%%)" % (fld, n, 100.0 * n / len(sub)))
        print("   статусы: " + ", ".join(
            "%s=%d" % (k or "(пусто)", v)
            for k, v in Counter(r["status"] for r in sub).most_common()))
        print("   quality: " + ", ".join(
            "%s=%d" % (k or "(пусто)", v)
            for k, v in Counter(r["quality"] for r in sub).most_common(8)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
