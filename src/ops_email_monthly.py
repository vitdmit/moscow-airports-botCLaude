"""Месячное письмо по операционной сводке: те же три таблицы, что в ежедневном,
но за календарный месяц и с колонками «прошлый месяц» вместо «среднее за N дн.».

Когда уходит. Ежедневный сбор берёт позавчерашние сутки. Если этот день последний
в месяце, значит в базе лежит весь месяц, и вторым письмом идёт сводка за него.
Для сентября это запуск 2 октября. Смена месяца ничем, кроме даты, не задаётся,
отдельного расписания нет.

Все суммы и проценты считает DuckDB, питон только раскладывает по HTML.
Перед записью письма итоги сверяются: сумма по аэропортам, зонам и терминалам
обязана совпасть с итогом по исходным строкам, иначе выход с ошибкой и письма нет.

Запуск:
    python -m src.ops_email_monthly --due     печатает yes или no и выходит
    python -m src.ops_email_monthly           собирает письмо, если оно положено
Переменные: FETCH_DATE (ручной сбор, как в daily_fetch), MONTH_FORCE=YYYY-MM
(собрать месяц принудительно, для проверки; письмо в этом случае не шлём).
Пишет /tmp/ops_month_body.html и в GITHUB_OUTPUT: send, subject, month.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
from datetime import date

from src.ops_email import CSS, NAMES, place
from src.ops_report import FIELDS, OPS_DIR, SCHEMA
from src.utils import day_before_yesterday_msk, get_logger

log = get_logger("ops-month")

MONTH_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
             "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
MONTH_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")

# Порог «хуже прошлого месяца»: в 1.3 раза и при заметном числе рейсов.
WORSE = 1.3
BETTER = 1 / 1.3
MIN_EVENTS = 10
# Разница меньше этого числа процентных пунктов цветом не отмечается.
MIN_PP_CANCEL = 0.5
MIN_PP_DELAY = 2.0


def target_day() -> date:
    raw = (os.environ.get("FETCH_DATE") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return day_before_yesterday_msk()


def is_last_day(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def report_month() -> tuple[int, int] | None:
    force = (os.environ.get("MONTH_FORCE") or "").strip()
    if force:
        y, m = (int(x) for x in force.split("-"))
        return y, m
    d = target_day()
    return (d.year, d.month) if is_last_day(d) else None


def prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def load_rows() -> tuple[list[tuple], int]:
    """Все строки сводок текущей схемы. Другие схемы не сопоставимы по порогам."""
    out = []
    files = 0
    for p in sorted(OPS_DIR.glob("*.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("не читается %s: %s", p, exc)
            continue
        if j.get("schema") != SCHEMA:
            continue
        files += 1
        for r in j.get("rows", []):
            out.append((j["date"][:7], j["date"], r["airport"], r["zone"],
                        r["terminal"]) + tuple(int(r.get(f, 0)) for f in FIELDS))
    return out, files


SUMS = ",".join("sum(%s) AS %s" % (f, f) for f in FIELDS)
BASE = "(sum(departed) - sum(no_fact))"
RATES = ("100.0 * sum(canceled) / nullif(sum(planned), 0) AS cpct, "
         "100.0 * sum(delayed_total) / nullif(%s, 0) AS dpct, "
         "sum(delay_min_total) * 1.0 / nullif(%s, 0) AS avg_min" % (BASE, BASE))


def roll(con, ym: str, keys: list[str], where: str = "") -> dict:
    """Суммы и доли по набору ключей за месяц ym (строка YYYY-MM)."""
    sel = (", ".join(keys) + ", ") if keys else ""
    grp = (" GROUP BY " + ", ".join(keys)) if keys else ""
    q = ("SELECT %s%s, %s FROM t WHERE ym = ? %s%s"
         % (sel, SUMS, RATES, where, grp))
    cur = con.execute(q, [ym])
    cols = [c[0] for c in cur.description]
    res = {}
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        key = tuple(rec[k] for k in keys)
        res[key] = rec
    return res


def n_days(con, ym: str) -> int:
    return con.execute("SELECT count(DISTINCT d) FROM t WHERE ym = ?", [ym]).fetchone()[0]


def per_day(con, ym: str, planned: int) -> float | None:
    n = n_days(con, ym)
    return None if not n else con.execute("SELECT ? * 1.0 / ?", [planned, n]).fetchone()[0]


def fmt_pct(v) -> str:
    return "н/д" if v is None else "%.1f%%" % v


def fmt_int(v) -> str:
    return "н/д" if v is None else "{:,.0f}".format(v).replace(",", " ")


def verify(con, ym: str, tabs: dict) -> None:
    """Каждый разрез должен дать те же суммы, что исходные строки месяца."""
    src = con.execute("SELECT %s FROM t WHERE ym = ?" % SUMS, [ym]).fetchone()
    for name, (rows, extra) in tabs.items():
        for i, f in enumerate(FIELDS):
            got = sum(r[f] for r in rows.values()) + extra.get(f, 0)
            if got != src[i]:
                raise SystemExit("сверка не сошлась: %s, поле %s: разрез %s, итог %s"
                                 % (name, f, got, src[i]))
    planned, canceled, diverted, departed = (src[FIELDS.index(x)] for x in
                                            ("planned", "canceled", "diverted", "departed"))
    if planned != canceled + diverted + departed:
        raise SystemExit("план %s не равен отмены+запасной+вылетели %s"
                         % (planned, canceled + diverted + departed))
    d1, d2, d3, dt = (src[FIELDS.index(x)] for x in
                      ("delay_60_120", "delay_120_180", "delay_180_plus", "delayed_total"))
    if d1 + d2 + d3 != dt:
        raise SystemExit("градации задержек %s не равны итогу %s" % (d1 + d2 + d3, dt))


def table(con, rows_out: list[tuple], label: str, cur_ym: str, prev_ym: str,
          prev_name: str, nd: int, pnd: int) -> str:
    h = ["<table><tr><th>%s</th><th>Запла-<br>нировано</th><th>В сутки</th>"
         "<th>В сутки,<br>%s</th><th>Отменено</th><th>Отменено,<br>%%</th>"
         "<th>Отмены,<br>%%, %s</th>"
         "<th>1-2<br>часа</th><th>2-3<br>часа</th><th>больше<br>3 часов</th>"
         "<th>Задержано<br>от часа, %%</th><th>Задержки,<br>%%, %s</th>"
         "<th>Средняя<br>задержка на<br>рейс, мин</th><th>Средняя,<br>%s, мин</th></tr>"
         % (label, prev_name, prev_name, prev_name, prev_name)]
    for i, (name, r, b, is_total) in enumerate(rows_out):
        cls = ' class="tot"' if is_total else (' class="zebra"' if i % 2 else "")
        pd_cur = per_day(con, cur_ym, r["planned"])
        pd_prev = per_day(con, prev_ym, b["planned"]) if b else None
        worse_c = (b is not None and r["cpct"] is not None and b["cpct"] is not None
                   and r["cpct"] > b["cpct"] * WORSE and r["cpct"] - b["cpct"] >= MIN_PP_CANCEL and r["canceled"] > MIN_EVENTS)
        worse_d = (b is not None and r["dpct"] is not None and b["dpct"] is not None
                   and r["dpct"] > b["dpct"] * WORSE and r["dpct"] - b["dpct"] >= MIN_PP_DELAY and r["delayed_total"] > MIN_EVENTS)
        better_c = (b is not None and r["cpct"] is not None and b["cpct"] is not None
                    and r["cpct"] < b["cpct"] * BETTER and b["cpct"] - r["cpct"] >= MIN_PP_CANCEL and b["canceled"] > MIN_EVENTS)
        better_d = (b is not None and r["dpct"] is not None and b["dpct"] is not None
                    and r["dpct"] < b["dpct"] * BETTER and b["dpct"] - r["dpct"] >= MIN_PP_DELAY and b["delayed_total"] > MIN_EVENTS)

        def pc(v, bad, good):
            t = fmt_pct(v)
            if bad:
                return '<td class="bad">%s</td>' % t
            if good:
                return '<td class="good">%s</td>' % t
            return "<td>%s</td>" % t

        avg = "н/д" if r["avg_min"] is None else "%.0f" % r["avg_min"]
        pavg = "н/д" if not b or b["avg_min"] is None else "%.0f" % b["avg_min"]
        h.append('<tr%s><td class="l">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                 "%s<td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s<td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (cls, name, fmt_int(r["planned"]), fmt_int(pd_cur), fmt_int(pd_prev),
                    fmt_int(r["canceled"]), pc(r["cpct"], worse_c, better_c),
                    fmt_pct(b["cpct"]) if b else "н/д",
                    fmt_int(r["delay_60_120"]), fmt_int(r["delay_120_180"]),
                    fmt_int(r["delay_180_plus"]),
                    pc(r["dpct"], worse_d, better_d),
                    fmt_pct(b["dpct"]) if b else "н/д", avg, pavg))
    h.append("</table>")
    return "".join(h)


def build(y: int, m: int) -> tuple[str, str]:
    import duckdb

    rows, files = load_rows()
    cur_ym = "%04d-%02d" % (y, m)
    py, pm = prev_month(y, m)
    prev_ym = "%04d-%02d" % (py, pm)

    con = duckdb.connect()
    con.execute("CREATE TABLE t (ym VARCHAR, d VARCHAR, airport VARCHAR, zone VARCHAR, "
                "terminal VARCHAR, %s)" % ", ".join("%s BIGINT" % f for f in FIELDS))
    con.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * (5 + len(FIELDS))), rows)
    loaded = con.execute("SELECT count(*) FROM t").fetchone()[0]
    if loaded != len(rows):
        raise SystemExit("в таблицу попало %d строк из %d" % (loaded, len(rows)))

    nd = n_days(con, cur_ym)
    if not nd:
        raise SystemExit("нет сводок за %s" % cur_ym)
    pnd = n_days(con, prev_ym)
    dim = calendar.monthrange(y, m)[1]
    pdim = calendar.monthrange(py, pm)[1]
    have = [r[0] for r in con.execute(
        "SELECT DISTINCT d FROM t WHERE ym = ? ORDER BY d", [cur_ym]).fetchall()]
    missing = [d for d in (date(y, m, i).isoformat() for i in range(1, dim + 1))
               if d not in have]
    pfirst, plast = (con.execute(
        "SELECT min(d), max(d) FROM t WHERE ym = ?", [prev_ym]).fetchone()
        if pnd else (None, None))

    # разрезы
    known = " AND zone <> '?'"
    tot = roll(con, cur_ym, [])[()]
    ptot = roll(con, prev_ym, []).get(())
    by_ap, p_ap = roll(con, cur_ym, ["airport"]), roll(con, prev_ym, ["airport"])
    by_z, p_z = (roll(con, cur_ym, ["airport", "zone"], known),
                 roll(con, prev_ym, ["airport", "zone"], known))
    svo = " AND zone <> '?' AND airport = 'SVO'"
    by_t, p_t = (roll(con, cur_ym, ["airport", "zone", "terminal"], svo),
                 roll(con, prev_ym, ["airport", "zone", "terminal"], svo))
    unknown = con.execute("SELECT coalesce(sum(planned), 0) FROM t WHERE ym = ? AND zone = '?'",
                          [cur_ym]).fetchone()[0]
    unknown_row = {f: 0 for f in FIELDS}
    for i, f in enumerate(FIELDS):
        unknown_row[f] = con.execute(
            "SELECT coalesce(sum(%s), 0) FROM t WHERE ym = ? AND zone = '?'" % f,
            [cur_ym]).fetchone()[0]
    svo_rest = {f: con.execute(
        "SELECT coalesce(sum(%s), 0) FROM t WHERE ym = ? AND NOT (airport = 'SVO' AND zone <> '?')" % f,
        [cur_ym]).fetchone()[0] for f in FIELDS}
    verify(con, cur_ym, {"аэропорты": (by_ap, {}), "зоны": (by_z, unknown_row),
                         "терминалы SVO": (by_t, svo_rest)})

    cur_name, prev_name = MONTH_NOM[m - 1], MONTH_NOM[pm - 1]
    parts = ["<style>%s</style>" % CSS,
             "<h2>Вылеты из аэропортов Москвы за %s %d</h2>" % (cur_name, y)]
    note = ("Данные нашего бота по расписанию вылетов. Задержкой считаем отклонение факта "
            "от плана на час и больше. Рейсы без факта отправления в базу для процента "
            "задержек не входят. В базе %d суток из %d" % (nd, dim))
    if missing:
        note += " (нет сводок за %s)" % ", ".join(
            "%d.%02d" % (int(d[8:]), int(d[5:7])) for d in missing)
    note += ". "
    if pnd and ptot:
        note += ("Сравнение с прошлым месяцем (%s %d): в базе %d суток из %d, с %s по %s. "
                 % (prev_name, py, pnd, pdim,
                    "%d %s" % (int(pfirst[8:]), MONTH_GEN[int(pfirst[5:7]) - 1]),
                    "%d %s" % (int(plast[8:]), MONTH_GEN[int(plast[5:7]) - 1])))
        if pnd < pdim:
            note += ("Остальные сутки месяца не собраны или посчитаны по другому источнику, "
                     "в сравнение они не берутся. Поэтому объёмы сравниваются по среднему "
                     "числу рейсов в сутки, а доли в процентах считаются за все доступные "
                     "сутки каждого месяца.")
    else:
        note += "Сводок за прошлый месяц нет, сравнение не строится."
    parts.append('<p class="note">%s</p>' % note)

    rows_out = [("Все три аэропорта", tot, ptot, True)]
    for (ap,), r in sorted(by_ap.items()):
        rows_out.append((NAMES.get(ap, ap), r, p_ap.get((ap,)), False))
    parts.append("<h2>Итого по аэропортам</h2>")
    parts.append(table(con, rows_out, "Аэропорт", cur_ym, prev_ym, prev_name, nd, pnd))

    parts.append("<h2>По зонам</h2>")
    rows_out = [(place(ap, z), r, p_z.get((ap, z)), False)
                for (ap, z), r in sorted(by_z.items())]
    parts.append(table(con, rows_out, "Аэропорт и зона", cur_ym, prev_ym, prev_name, nd, pnd))

    parts.append("<h2>Шереметьево по терминалам</h2>")
    rows_out = [(place(ap, z, t), r, p_t.get((ap, z, t)), False)
                for (ap, z, t), r in sorted(by_t.items())]
    parts.append(table(con, rows_out, "Зона и терминал", cur_ym, prev_ym, prev_name, nd, pnd))
    if unknown:
        parts.append('<p class="note">Без распознанной зоны %s рейсов: они входят в итог по '
                     "аэропортам, но не в таблицы по зонам.</p>" % fmt_int(unknown))

    cp = fmt_pct(tot["cpct"])
    dp = fmt_pct(tot["dpct"])
    subject = ("Вылеты за %s %d: запланировано %s, отменено %s (%s), задержано от часа %s"
               % (cur_name, y, fmt_int(tot["planned"]), fmt_int(tot["canceled"]), cp, dp))
    return "".join(parts), subject.replace(" ", " ")


def main() -> int:
    due = report_month()
    if "--due" in sys.argv:
        print("yes" if due else "no")
        return 0
    out = os.environ.get("GITHUB_OUTPUT")

    def emit(**kw):
        if out:
            with open(out, "a", encoding="utf-8") as f:
                for k, v in kw.items():
                    f.write("%s=%s\n" % (k, v))

    if not due:
        log.info("Целевой день %s не последний в месяце, месячное письмо не нужно",
                 target_day())
        emit(send="false")
        return 0
    y, m = due
    body, subject = build(y, m)
    with open("/tmp/ops_month_body.html", "w", encoding="utf-8") as f:
        f.write(body)
    forced = bool((os.environ.get("MONTH_FORCE") or "").strip())
    emit(send="false" if forced else "true", subject=subject, month="%04d-%02d" % (y, m))
    log.info("Тема: %s", subject)
    log.info("Тело: /tmp/ops_month_body.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
