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

# Порог задержки: час. Не 15 минут, и вот почему.
#
# Диагностика 03.09.2026 (src/diag_times.py, data/diag/times_2026-09-01.csv)
# показала, что три аэропорта присылают разные события в одном поле:
#   Шереметьево: отход от гейта, есть отдельно и отрыв от полосы
#     (руление по медиане 16 минут), раньше плана уходит 47% рейсов;
#   Домодедово: отход от гейта, но обновление публикуется только когда рейс
#     опоздал: раньше плана 4%, а хвост обрезан, за июнь-август максимум
#     111 минут при 8928 рейсах;
#   Внуково: похоже на отрыв от полосы, раньше плана 1%, ровно по плану 0%.
#
# Проверка на Шереметьеве, где есть оба времени: при пороге 15 минут выбор
# события меняет ответ на 40.5 процентных пункта, при пороге 60 минут на 2.8.
# То есть крупные задержки все три аэропорта меряют почти одинаково, а мелкие
# несопоставимы в принципе, никакой вычитаемой константой это не лечится.
#
# Раньше здесь стоял TAXI_OFFSET с вычитанием медианы аэропорта. Убран:
# медиана содержит и реальные опоздания, вычитание стирало их вместе с
# рулением.
DELAY_MIN = 60

# Градации задержки в минутах: имя, от (включительно), до (не включая).
DELAY_BUCKETS = (("60_120", 60, 120), ("120_180", 120, 180), ("180_plus", 180, 10 ** 9))

# Версия схемы строки. Меняется, когда меняется состав или смысл полей.
# ops_email берёт в базу для средних только сводки текущей версии.
SCHEMA = 2

FIELDS = ("planned", "canceled", "diverted", "delay_60_120", "delay_120_180",
          "delay_180_plus", "delayed_total", "on_time", "departed", "no_fact",
          "delay_min_total")


def _terminal_of(dep: dict) -> str:
    t = dep.get("terminal")
    t = str(t).strip() if t is not None else ""
    return t or "н/д"


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def zone_by_place(airport: str, terminal: str, gate: str):
    """Зона по месту вылета. Возвращает 'ВВЛ', 'МВЛ' или None, если правила нет.

    Место вылета важнее направления: пассажир сидит там, где сидит, а поле
    направления у API врёт на стыковочных рейсах. Пример: NordStar Y7 533 из
    Домодедова уходит с гейта E (международная зона), а направление приходит
    24 раза как Красноярск и 2 раза как Тайюань. Это один и тот же рейс с
    промежуточной посадкой, пассажиры проходят границу в Домодедове.

    Проверено по data/daily за май-сентябрь 2026:
      SVO: гейты 1-123 это терминал B, 124-146 терминал C, терминал D свой.
           В терминале C 10 538 рейсов, международных из них 9627 в полосе
           гейтов 126-145, а в терминалах B и D международных нет ни одного.
      DME: из гейтов E ушло 2936 международных, из гейтов C и D ни одного.
      VKO: терминал A обслуживает обе зоны, правила нет.
    """
    t = (terminal or "").strip().upper()
    g = (gate or "").strip().upper()
    if airport == "SVO":
        if t == "C":
            return "МВЛ"
        if t in ("B", "D"):
            return "ВВЛ"
    elif airport == "DME":
        if g.startswith("E") or t == "E":
            return "МВЛ"
        if g[:1] in ("C", "D") or t in ("C", "D"):
            return "ВВЛ"
    return None


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

            # ВАЖНО. Задержку считаем ТОЛЬКО по revisedTime — это фактическое
            # время отправления от гейта. runwayTime это отрыв от полосы, он
            # больше планового на время руления (во Внукове медиана 31 минута),
            # и по нему 80% рейсов ложно выглядят задержанными.
            # Если revisedTime нет, рейс в базу задержек не идёт: считаем его
            # в no_fact и в проценте задержек не учитываем.
            revised = _parse_local((dep.get("revisedTime") or {}).get("local"))
            delay = None
            if revised is not None:
                delay = _minutes(revised) - _minutes(sched)
                if delay > 720:
                    delay -= 1440
                elif delay <= -720:
                    delay += 1440

            status = (item.get("status") or "").strip().lower()
            g = groups.get(key)
            if g is None:
                g = {"sched": sched.strftime("%H:%M"), "dest": dest,
                     "terminal": "н/д", "gate": "", "status": "", "delay": None}
                groups[key] = g
            if g["terminal"] == "н/д":
                g["terminal"] = _terminal_of(dep)
            if not g["gate"]:
                g["gate"] = str(dep.get("gate") or "").strip()
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
        # У DME терминал отдельным полем не приходит, берём первую букву гейта
        # (D13 -> D). Так же делает основной сбор.
        term = r["terminal"]
        if term == "н/д" and r.get("gate"):
            first = r["gate"][0].upper()
            if first.isalpha():
                term = first
        # Зона: сначала по месту вылета, направление только если правила нет.
        z = zone_by_place(airport, term, r.get("gate")) or zone(r["dest"]) or "?"
        # В Домодедово международная зона это только терминал E. Проверено по
        # data/daily за июнь-сентябрь: из гейтов E ушло 2936 международных
        # рейсов, из гейтов C и D ни одного. Гейт есть не у всех рейсов
        # (у отменённых его нет вовсе), поэтому у части МВЛ терминал получался
        # "н/д" и Домодедово разваливалось на две строки вместо одной.
        if airport == "DME" and z == "МВЛ":
            term = "E"
        key = (z, term)
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
        if d is None:
            a["no_fact"] += 1
            continue
        if d > 0:
            a["delay_min_total"] += d
        if d < DELAY_MIN:
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
        # Сутки разделены раньше, в fetch_airport_day по окнам запроса: в ответе
        # API лежат и вчерашние рейсы, уехавшие за полночь, и утренние
        # рейсы следующих суток. До правки 03.09.2026 они попадали в сводку.
        rows.extend(summarize(ap, collapse(payloads)))
    if not rows:
        log.error("Сводка за %s пустая, нечего писать", day)
        return ""
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    path = OPS_DIR / ("%s.json" % day.isoformat())
    path.write_text(json.dumps({"date": day.isoformat(), "schema": SCHEMA,
                                "rows": rows},
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
