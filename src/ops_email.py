"""Письмо по операционной сводке: тело в HTML плюс CSV во вложении.

Читает data/ops_daily/<дата>.json, считает средние за последние 30 доступных
дней и складывает два файла:
    /tmp/ops_body.html   — тело письма
    /tmp/ops_<дата>.csv  — полный разрез до терминалов, вложение
Плюс печатает строку темы письма в GITHUB_OUTPUT, если он есть.

Запуск: OPS_DATE=2026-09-01 python -m src.ops_email
Без OPS_DATE берётся самая свежая сводка.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date

from src.ops_report import FIELDS, OPS_DIR, SCHEMA, load_days
from src.utils import get_logger

log = get_logger("ops-email")

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
NAMES = {"SVO": "Шереметьево", "VKO": "Внуково", "DME": "Домодедово"}

CSS = """
body{font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1a1a1a}
h2{color:#1b5e20;font-size:16px;margin:18px 0 6px}
p.note{color:#6b6b6b;font-size:12px;margin:4px 0}
table{border-collapse:collapse;margin:6px 0 14px}
th{background:#1b5e20;color:#fff;font-weight:600;padding:5px 8px;
   border:1px solid #ddd;font-size:12px;text-align:center}
td{border:1px solid #ddd;padding:4px 8px;text-align:right;font-size:12px}
td.l{text-align:left}
tr.zebra td{background:#f5f7f4}
tr.tot td{font-weight:600;background:#eef2ec}
.bad{color:#c62828}
.good{color:#1b7a3d}
"""


def human(d: date) -> str:
    return "%d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


def pct(part: int, whole: int) -> str:
    return "0.0%" if not whole else "%.1f%%" % (100.0 * part / whole)


def add(a: dict, b: dict) -> dict:
    for f in FIELDS:
        a[f] = a.get(f, 0) + b.get(f, 0)
    return a


def zero() -> dict:
    return {f: 0 for f in FIELDS}


def rollup(rows: list[dict], keys) -> dict:
    acc: dict = defaultdict(zero)
    for r in rows:
        acc[tuple(r[k] for k in keys)] = add(acc[tuple(r[k] for k in keys)], r)
    return acc


def avg_over(days: list[dict], keys, skip_unknown: bool = False) -> dict:
    """Суммы за набор предыдущих дней, из них считаются средние доли."""
    acc: dict = defaultdict(zero)
    for d in days:
        for r in d["rows"]:
            if skip_unknown and r["zone"] == "?":
                continue
            acc[tuple(r[k] for k in keys)] = add(acc[tuple(r[k] for k in keys)], r)
    return acc


def place(ap: str, zone_name: str, terminal: str = "") -> str:
    """Подпись строки. Терминал н/д не пишем: в Домодедово источник его не даёт."""
    s = "%s, %s" % (NAMES.get(ap, ap), zone_name)
    if terminal and terminal != "н/д":
        s += ", терминал %s" % terminal
    return s


def cell(v: str, bad: bool = False) -> str:
    return '<td class="bad">%s</td>' % v if bad else "<td>%s</td>" % v


def table(rows_out: list[tuple], base: dict, keys_label: str, ndays: int) -> str:
    h = ["<table><tr>",
         "<th>%s</th><th>Запла-<br>нировано</th><th>Отменено</th><th>Отменено,<br>%%</th>"
         "<th>Отмены,<br>среднее<br>за %d дн.</th>"
         "<th>1-2<br>часа</th><th>2-3<br>часа</th><th>больше<br>3 часов</th>"
         "<th>Задержано<br>от часа, %%</th>"
         "<th>Задержки,<br>среднее<br>за %d дн.</th>"
         "<th>Средняя<br>задержка<br>на рейс, мин</th>"
         "</tr>" % (keys_label, ndays, ndays)]
    for i, (label, r, b, is_total) in enumerate(rows_out):
        cls = ' class="tot"' if is_total else (' class="zebra"' if i % 2 else "")
        base_r = r["departed"] - r["no_fact"]
        bbase = (b["departed"] - b["no_fact"]) if b else 0
        bp = pct(b["canceled"], b["planned"]) if b and b["planned"] else "н/д"
        bd = pct(b["delayed_total"], bbase) if bbase else "н/д"
        cp = 100.0 * r["canceled"] / r["planned"] if r["planned"] else 0
        cd = 100.0 * r["delayed_total"] / base_r if base_r else 0
        bpv = 100.0 * b["canceled"] / b["planned"] if b and b["planned"] else None
        bdv = 100.0 * b["delayed_total"] / bbase if bbase else None
        h.append("<tr%s><td class=\"l\">%s</td><td>%d</td><td>%d</td>%s<td>%s</td>"
                 "<td>%d</td><td>%d</td><td>%d</td>%s<td>%s</td><td>%s</td></tr>"
                 % (cls, label, r["planned"], r["canceled"],
                    cell(pct(r["canceled"], r["planned"]),
                         bpv is not None and cp > bpv * 1.3 and r["canceled"] > 2),
                    bp,
                    r["delay_60_120"], r["delay_120_180"], r["delay_180_plus"],
                    cell(pct(r["delayed_total"], base_r),
                         bdv is not None and cd > bdv * 1.3 and r["delayed_total"] > 5),
                    bd, "0" if not base_r else "%.0f" % (r["delay_min_total"] / base_r)))
    h.append("</table>")
    return "".join(h)


def build(day: date) -> tuple[str, str, str]:
    days = load_days(day, limit=31)
    if not days or days[0]["date"] != day.isoformat():
        raise SystemExit("нет сводки за %s в %s" % (day, OPS_DIR))
    today_rows = days[0]["rows"]
    # В базу для средних берём только сводки текущей схемы. Старые считались
    # по порогу 15 минут и с поправкой на руление, смешивать их нельзя.
    base_days = [d for d in days[1:] if d.get("schema") == SCHEMA]
    ndays = len(base_days)

    # Строки с нераспознанным направлением в разрезы по зонам не пускаем:
    # это единичные грузовые борта без пункта назначения. В итог по
    # аэропортам они входят, под таблицей стоит сноска с их числом.
    known = [r for r in today_rows if r["zone"] != "?"]
    unknown_planned = sum(r["planned"] for r in today_rows if r["zone"] == "?")

    by_ap = rollup(today_rows, ["airport"])
    by_ap_zone = rollup(known, ["airport", "zone"])
    # Разрез по терминалам только по Шереметьеву. Во Внукове всё в терминале A,
    # терминал там равен зоне. В Домодедове терминал C закрывается, останутся
    # D на всю ВВЛ и E на всю МВЛ, то есть тоже одно и то же. Дробить нечего.
    by_full = rollup([r for r in known if r["airport"] == "SVO"],
                     ["airport", "zone", "terminal"])
    b_ap = avg_over(base_days, ["airport"])
    b_ap_zone = avg_over(base_days, ["airport", "zone"], skip_unknown=True)
    b_full = avg_over(base_days, ["airport", "zone", "terminal"], skip_unknown=True)

    total = zero()
    for r in today_rows:
        add(total, r)
    b_total = zero()
    for d in base_days:
        for r in d["rows"]:
            add(b_total, r)

    parts = ["<style>%s</style>" % CSS,
             "<h2>Вылеты из аэропортов Москвы, %s</h2>" % human(day)]
    parts.append('<p class="note">Данные нашего бота по расписанию вылетов. '
                 'Задержкой считаем отклонение факта от плана на час и больше. '
                 'Рейсы, по которым источник не дал факта отправления, в базу для '
                 'процента задержек не берутся. '
                 'Столбцы «среднее» по %d предыдущим суткам, которые есть в базе.</p>'
                 % ndays if ndays else
                 '<p class="note">Данные нашего бота по расписанию вылетов. '
                 'Задержкой считаем отклонение факта от плана на час и больше. '
                 'База для сравнения пока не накоплена, столбцы «среднее» пустые.</p>')

    rows_out = [("Все три аэропорта", total, b_total, True)]
    for (ap,), r in sorted(by_ap.items()):
        rows_out.append((NAMES.get(ap, ap), r, b_ap.get((ap,)), False))
    parts.append("<h2>Итого по аэропортам</h2>")
    parts.append(table(rows_out, b_total, "Аэропорт", ndays))

    parts.append("<h2>По зонам</h2>")
    rows_out = []
    for (ap, z), r in sorted(by_ap_zone.items()):
        rows_out.append((place(ap, z), r, b_ap_zone.get((ap, z)), False))
    parts.append(table(rows_out, b_total, "Аэропорт и зона", ndays))

    parts.append("<h2>Шереметьево по терминалам</h2>")
    rows_out = []
    for (ap, z, t), r in sorted(by_full.items()):
        rows_out.append((place(ap, z, t), r, b_full.get((ap, z, t)), False))
    parts.append(table(rows_out, b_total, "Зона и терминал", ndays))

    body = "".join(parts)
    csv_path = "/tmp/ops_%s.csv" % day.isoformat()
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["дата", "аэропорт", "зона", "терминал", "запланировано",
                    "отменено", "отменено_%", "ушли_на_запасной",
                    "задержка_1_2ч", "задержка_2_3ч", "задержка_больше_3ч",
                    "задержано_от_часа", "задержано_%", "вылетело",
                    "без_задержки_от_часа_%", "без_факта_вылета",
                    "сумма_минут_задержки", "средняя_задержка_на_рейс_мин"])
        for (ap, z, t), r in sorted(by_full.items()):
            b_r = r["departed"] - r["no_fact"]
            w.writerow([day.isoformat(), ap, z, t, r["planned"], r["canceled"],
                        pct(r["canceled"], r["planned"]), r["diverted"],
                        r["delay_60_120"], r["delay_120_180"], r["delay_180_plus"],
                        r["delayed_total"], pct(r["delayed_total"], b_r),
                        r["departed"], pct(r["on_time"], b_r), r["no_fact"],
                        r["delay_min_total"],
                        0 if not b_r else round(r["delay_min_total"] / b_r)])
    subject = ("Вылеты %s: запланировано %d, отменено %d (%s), задержано от часа %s"
               % (day.strftime("%d.%m.%Y"), total["planned"], total["canceled"],
                  pct(total["canceled"], total["planned"]),
                  pct(total["delayed_total"], total["departed"] - total["no_fact"])))
    return body, csv_path, subject


def main() -> int:
    ds = (os.environ.get("OPS_DATE") or "").strip()
    if ds:
        day = date.fromisoformat(ds)
    else:
        days = load_days(date.today(), limit=1)
        if not days:
            log.error("нет ни одной сводки в %s", OPS_DIR)
            return 1
        day = date.fromisoformat(days[0]["date"])
    body, csv_path, subject = build(day)
    with open("/tmp/ops_body.html", "w", encoding="utf-8") as f:
        f.write(body)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("subject=%s\n" % subject)
            f.write("csv=%s\n" % csv_path)
            f.write("day=%s\n" % day.isoformat())
    log.info("Тема: %s", subject)
    log.info("Тело: /tmp/ops_body.html, вложение: %s", csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
