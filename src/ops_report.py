"""Ежедневная операционная сводка по трём аэропортам.

Что считает за сутки, в разрезе аэропорт / зона ВВЛ-МВЛ / терминал:
  запланировано вылетов, отменено, ушло на запасной, задержано
  (три градации: 15-60 минут, 1-3 часа, больше 3 часов), вылетело вовремя.

Откуда данные: те же ответы AeroDataBox, что уже приходят для ежедневного
сбора рейсов. Дополнительных запросов к API НЕ делает: payload'ы остаются
в src.aerodatabox.LAST_PAYLOADS после fetch_airport_day.

Что пишет: data/ops_daily/<дата>.json — одна строка на связку
аэропорт-зона-терминал. data/daily не трогает, чтобы не ломать аналитику
гейтов (там по-прежнему только фактически вылетевшие рейсы).

Кодшеринги схлопываются так же, как в основном сборе: один физический борт
это одна строка (ключ = плановое время суток + нормализованное направление).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from src.aerodatabox import LAST_PAYLOADS, _norm_dest, _parse_local
from src.config import DATA_DIR
from src.utils import get_logger
from src.zones import zone

log = get_logger("ops")

OPS_DIR = DATA_DIR / "ops_daily"

CANCEL_STATUSES = {"canceled", "cancelled", "canceleduncertain"}
DIVERT_STATUSES = {"diverted"}

# Градации задержки в минутах: имя, от (включительно), до (не включая).
DELAY_BUCKETS = (("15_60", 15, 60), ("60_180", 60, 180), ("180_plus", 180, 10 ** 9))

FIELDS = ("planned", "canceled", "diverted", "delay_15_60", "delay_60_180",
          "delay_180_plus", "delayed_total", "on_time", "departed")


def _terminal_of(dep: dict) -> str:
    t = dep.get("terminal")
    t = str(t).strip() if t is not None else ""
    return t or "н/д"


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def collapse(payloads: list[dict]) -> list[dict]:
    """Схлопнуть кодшеринги: один борт = одна запись."""
    groups: dict[tuple, dict] = {}
    for payload in payloads or []:
        for item in (payload.get("departures") or []):
            if item.get("isCargo"):
                continue
            dep = item.get("movement") or item.get("departure") or {}
            sched = _parse_local((dep.get("scheduledTime") or {}).get("local"))
            if sched is None:
                continue
            arr = dep.get("airport") or (item.get("arrival") or {}).get("airport") or {}
            dest = arr.get("name") or arr.get("iata") or ""
            iata = arr.get("iata") or ""
            key = (sched.strftime("%H:%M"), _norm_dest(dest, iata))

            revised = _parse_local((dep.get("revisedTime") or {}).get("local"))
            runway = _parse_local((dep.get("runwayTime") or {}).get("local"))
            fact = runway or revised
            delay = None
            if fact is not None:
                delay = _minutes(fact) - _minutes(sched)
                if delay > 720:
                    delay -= 1440
                elif delay <= -720:
                    delay += 1440

            status = (item.get("status") or "").strip().lower()
            g = groups.get(key)
            if g is None:
                g = {"sched": sched.strftime("%H:%M"), "dest": dest,
                     "terminal": "н/д", "status": "", "delay": None}
                groups[key] = g
            if g["terminal"] == "н/д":
                g["terminal"] = _terminal_of(dep)
            # отмена и уход на запасной перебивают любой другой статус
            if status in CANCEL_STATUSES or status in DIVERT_STATUSES:
                g["status"] = status
            elif status and g["status"] not in CANCEL_STATUSES | DIVERT_STATUSES:
                g["status"] = status
            if delay is not None and (g["delay"] is None or delay > g["delay"]):
                g["delay"] = delay
    return list(groups.values())


def summarize(airport: str, rows: list[dict]) -> list[dict]:
    """Свернуть записи в строки аэропорт-зона-терминал."""
    acc: dict[tuple, dict] = defaultdict(lambda: {f: 0 for f in FIELDS})
    for r in rows:
        z = zone(r["dest"]) or "?"
        key = (z, r["terminal"])
        a = acc[key]
        a["planned"] += 1
        st = r["status"]
        if st in CANCEL_STATUSES:
            a["canceled"] += 1
            continue
        if st in DIVERT_STATUSES:
            a["diverted"] += 1
            continue
        a["departed"] += 1
        d = r["delay"]
        if d is None or d < 15:
            a["on_time"] += 1
            continue
        a["delayed_total"] += 1
        for name, lo, hi in DELAY_BUCKETS:
            if lo <= d < hi:
                a["delay_" + name] += 1
                break
    out = []
    for (z, term), a in sorted(acc.items()):
        row = {"airport": airport, "zone": z, "terminal": term}
        row.update(a)
        out.append(row)
    return out


def build_ops_day(day: date, airports=("SVO", "VKO", "DME")) -> str:
    """Собрать сводку за день из payload'ов последнего сбора и записать JSON."""
    rows: list[dict] = []
    for ap in airports:
        payloads = LAST_PAYLOADS.get(ap)
        if not payloads:
            log.warning("[%s] нет payload'ов за %s, аэропорт пропущен", ap, day)
            continue
        rows.extend(summarize(ap, collapse(payloads)))
    if not rows:
        log.error("Сводка за %s пустая, нечего писать", day)
        return ""
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    path = OPS_DIR / ("%s.json" % day.isoformat())
    path.write_text(json.dumps({"date": day.isoformat(), "rows": rows},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    tot = sum(r["planned"] for r in rows)
    can = sum(r["canceled"] for r in rows)
    dly = sum(r["delayed_total"] for r in rows)
    log.info("Сводка за %s: запланировано %d, отменено %d, задержано %d -> %s",
             day, tot, can, dly, path)
    return str(path)


def load_days(upto: date, limit: int = 31) -> list[dict]:
    """Прочитать последние сводки (включая upto), новые первыми."""
    out = []
    if not OPS_DIR.exists():
        return out
    for p in sorted(OPS_DIR.glob("*.json"), reverse=True):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d > upto:
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("не читается %s: %s", p, exc)
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys
    print("Модуль вызывается из src.daily_fetch, отдельный запуск не нужен.")
    sys.exit(0)
