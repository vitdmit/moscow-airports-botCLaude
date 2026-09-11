"""Недельный отчёт по вылетам: цифры в JSON и дашборд PDF в палитре «Контраст».

Два режима.

1. Выгрузка фактов. Считает все разрезы за отчётную неделю, за предыдущую
   и историю по всем сводкам схемы 2, кладёт в JSON и выходит.
       WEEK_FACTS=/tmp/facts.json WEEK_END=2026-09-13 python -m src.weekly_report
   Этот режим нужен вторничной задаче: Клод читает факты, пишет разбор.

2. Сборка дашборда.
       WEEK_ANALYSIS=/tmp/analysis.json WEEK_END=2026-09-13 python -m src.weekly_report
   Складывает /tmp/weekly_<конец>.pdf и /tmp/weekly_body.html.
   Без WEEK_ANALYSIS текстовые блоки собираются шаблоном из цифр.

Формат файла разбора:
    {"intro": "три предложения с цифрами",
     "blocks": [{"title": "Что изменилось к прошлой неделе",
                 "lines": ["строка с цифрой", "ещё строка"]}]}

Отчётная неделя это понедельник-воскресенье. Без WEEK_END берётся последняя,
у которой есть все семь сводок.

Сравнение с предыдущей неделей в таблицах рисуется, только если она закрыта
семью сводками схемы 2. Если базы нет, столбцы динамики просто не рисуются.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import duckdb

from src.config import DATA_DIR
from src.ops_report import OPS_DIR, SCHEMA
from src.utils import get_logger

log = get_logger("weekly")

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
NAMES = {"SVO": "Шереметьево", "VKO": "Внуково", "DME": "Домодедово"}
WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

POINTS_FILE = DATA_DIR / "points_zones.json"
HISTORY_DAYS = 84

# Палитра «Контраст»
C_TEXT = "#1a1a1a"
C_MUTED = "#6b6b6b"
C_HEAD = "#1b5e20"
C_UP = "#c62828"      # рост задержек и отмен это ухудшение
C_DOWN = "#1b7a3d"
C_BORD = "#dddddd"
C_ZEBRA = "#f5f7f4"
C_AXIS = "#cccccc"


# ---------------------------------------------------------------- данные

def read_rows(days: list[date]) -> list[dict]:
    out = []
    for d in days:
        p = OPS_DIR / ("%s.json" % d.isoformat())
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        if j.get("schema") != SCHEMA:
            continue
        for r in j["rows"]:
            r = dict(r)
            r["date"] = j["date"]
            out.append(r)
    return out


def week_days(end: date) -> list[date]:
    return [end - timedelta(days=i) for i in range(6, -1, -1)]


def latest_closed_week() -> date:
    """Воскресенье последней недели, у которой есть все семь сводок схемы 2."""
    d = date.today()
    d -= timedelta(days=(d.weekday() + 1) % 7)  # ближайшее прошедшее воскресенье
    for _ in range(8):
        if all((OPS_DIR / ("%s.json" % x.isoformat())).exists() for x in week_days(d)):
            return d
        d -= timedelta(days=7)
    return d


class Agg:
    """Все разрезы периода. Считает DuckDB, руками ничего не складываем."""

    def __init__(self, rows: list[dict]):
        self.ok = bool(rows)
        self.con = duckdb.connect()
        path = "/tmp/wk_rows_%d.json" % id(self)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows or [{}], f, ensure_ascii=False)
        if rows:
            self.con.execute(
                "CREATE TABLE t AS SELECT * FROM read_json_auto('%s')" % path)

    M = ("sum(planned) AS planned, sum(canceled) AS canceled, "
         "sum(departed) - sum(no_fact) AS base, sum(delayed_total) AS dly, "
         "sum(delay_60_120) AS b1, sum(delay_120_180) AS b2, "
         "sum(delay_180_plus) AS b3, sum(delay_min_total) AS dmin, "
         "sum(no_fact) AS no_fact, sum(diverted) AS diverted")

    def _q(self, sql: str) -> list[dict]:
        if not self.ok:
            return []
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["cpct"] = round(100.0 * d["canceled"] / d["planned"], 2) if d["planned"] else 0.0
            d["dpct"] = round(100.0 * d["dly"] / d["base"], 2) if d["base"] else 0.0
            d["avg"] = round(d["dmin"] / d["base"], 1) if d["base"] else 0.0
            out.append(d)
        return out

    def total(self) -> dict:
        r = self._q("SELECT %s FROM t" % self.M)
        return r[0] if r else {}

    def by_day(self) -> list[dict]:
        return self._q("SELECT CAST(date AS VARCHAR) AS date, %s FROM t "
                       "GROUP BY 1 ORDER BY 1" % self.M)

    def by_airport(self) -> list[dict]:
        return self._q("SELECT airport, %s FROM t GROUP BY 1 ORDER BY 1" % self.M)

    def by_airport_day(self) -> list[dict]:
        return self._q("SELECT airport, CAST(date AS VARCHAR) AS date, %s FROM t "
                       "GROUP BY 1,2 ORDER BY 1,2" % self.M)

    def by_zone(self) -> list[dict]:
        return self._q("SELECT airport, zone, %s FROM t WHERE zone <> '?' "
                       "GROUP BY 1,2 ORDER BY 1,2" % self.M)

    def svo_terminals(self) -> list[dict]:
        return self._q("SELECT zone, terminal, %s FROM t WHERE airport = 'SVO' "
                       "AND zone <> '?' GROUP BY 1,2 ORDER BY 1,2" % self.M)

    def by_weekday(self) -> list[dict]:
        return self._q("SELECT dayofweek(CAST(date AS DATE)) AS wd, %s FROM t "
                       "GROUP BY 1 ORDER BY 1" % self.M)

    def unknown(self) -> int:
        r = self._q("SELECT %s FROM t WHERE zone = '?'" % self.M)
        return r[0]["planned"] if r else 0

    def days(self) -> list[str]:
        return [r["date"] for r in self.by_day()]


def all_schema2_days(upto: date, limit: int = HISTORY_DAYS) -> list[date]:
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
        out.append(d)
        if len(out) >= limit:
            break
    return sorted(out)


def load_points() -> dict:
    if POINTS_FILE.exists():
        try:
            return json.loads(POINTS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("не читается %s: %s", POINTS_FILE, exc)
    return {}


# ---------------------------------------------------------------- факты

def collect_facts(end: date) -> dict:
    days = week_days(end)
    prev_days = week_days(end - timedelta(days=7))
    cur = Agg(read_rows(days))
    if not cur.ok:
        raise SystemExit("нет ни одной сводки схемы %d за неделю по %s"
                         % (SCHEMA, end))
    prev = Agg(read_rows(prev_days))
    hist = Agg(read_rows(all_schema2_days(end)))

    def cut(a: Agg) -> dict:
        return {"days": a.days(),
                "total": a.total(),
                "by_day": a.by_day(),
                "by_airport": a.by_airport(),
                "by_airport_day": a.by_airport_day(),
                "by_zone": a.by_zone(),
                "svo_terminals": a.svo_terminals(),
                "unknown_planned": a.unknown()}

    return {"week_end": end.isoformat(),
            "period": period([date.fromisoformat(d) for d in cur.days()]),
            "schema": SCHEMA,
            "current": cut(cur),
            "previous": cut(prev) if prev.ok else None,
            "history": {"days": hist.days(),
                        "by_airport": hist.by_airport(),
                        "by_zone": hist.by_zone(),
                        "svo_terminals": hist.svo_terminals(),
                        "by_weekday": hist.by_weekday(),
                        "by_day": hist.by_day()},
            "points": load_points()}


# ---------------------------------------------------------------- вёрстка

def human(d: date) -> str:
    return "%d %s" % (d.day, MONTHS[d.month - 1])


def period(days: list[date]) -> str:
    if not days:
        return ""
    a, b = days[0], days[-1]
    if a.month == b.month:
        return "%d-%d %s %d" % (a.day, b.day, MONTHS[a.month - 1], b.year)
    return "%s - %s %d" % (human(a), human(b), b.year)


def num(v: float, digits: int = 1) -> str:
    return ("%%.%df" % digits % v).replace(".", ",")


def sign(v: float, digits: int = 1, unit: str = "") -> str:
    s = "+" if v > 0 else ("−" if v < 0 else "")
    return "%s%s%s" % (s, num(abs(v), digits), unit)


def delta_html(cur: float, prev, digits=1, unit=" п.п.", worse_up=True) -> str:
    if prev is None:
        return ""
    d = cur - prev
    if abs(d) < 0.05 and digits <= 1:
        return '<span class="d flat">0%s</span>' % unit
    bad = (d > 0) if worse_up else (d < 0)
    return '<span class="d %s">%s</span>' % ("up" if bad else "down",
                                             sign(d, digits, unit))


def spark(values: list[float], w: int = 150, h: int = 26) -> str:
    """Спарклайн по дням. Ось снизу, значения нормируются на максимум."""
    if not values:
        return ""
    top = max(values) or 1.0
    n = len(values)
    step = w / max(n - 1, 1)
    pts = " ".join("%.1f,%.1f" % (i * step, h - 2 - (v / top) * (h - 6))
                   for i, v in enumerate(values))
    last_x = (n - 1) * step
    last_y = h - 2 - (values[-1] / top) * (h - 6)
    return ('<svg class="sp" viewBox="0 0 %d %d" preserveAspectRatio="none" '
            'style="width:100%%;height:%dpx;display:block">'
            '<line x1="0" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1"/>'
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6"/>'
            '<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/></svg>'
            % (w, h, h, h - 1, w, h - 1, C_AXIS, pts, C_HEAD,
               last_x, last_y, C_HEAD))


# Формулировки собираются шаблонами, ИИ в GitHub Actions не работает.
# Во вторничной задаче разбор пишет Клод и кладёт в WEEK_ANALYSIS, этот же
# список ловит нейрослоп в его тексте: сборка падает, текст переписывается.
SLOP = ("—", "важно отметить", "стоит подчеркнуть", "следует учитывать",
        "в современном мире", "ключевым фактором", "играет важную роль",
        "представляет собой", "не только", "комплексный подход", "синергия",
        "в свою очередь", "таким образом", "данный ", "данная ", "данную ",
        "осуществляется", "производится", "подводя итог", "в целом",
        "при этом", "стоит отметить", "разогнался", "провалился",
        "картина обратная", "остаётся ядром", "ушло глубже",
        "разошлась с фоном", "стоит на месте", "необходимо отметить")


def check_slop(text: str) -> None:
    """Длинное тире и обороты из списка anti-slop. Находит — валит сборку."""
    import re as _re
    plain = _re.sub(r"<style.*?</style>", " ", text, flags=_re.S)
    plain = _re.sub(r"<[^>]+>", " ", plain).lower()
    hits = [w for w in SLOP if w in plain]
    if hits:
        raise SystemExit("в тексте отчёта нейрослоп: %s" % ", ".join(hits))


CSS = """
@page { size: A4 portrait; margin: 13mm 12mm 11mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI","DejaVu Sans",sans-serif; font-size: 8.6pt;
       color: %(text)s; margin: 0; }
.kicker { font-size: 7pt; letter-spacing: 1.6px; color: %(muted)s;
          text-transform: uppercase; }
h1 { font-size: 20pt; font-weight: 300; margin: 3px 0 0; }
.sub { font-size: 8.4pt; color: %(muted)s; margin-top: 2px; }
.rule { height: 2px; background: %(head)s; margin: 8px 0 10px; }
.intro { font-size: 9pt; line-height: 1.45; margin: 0 0 10px; max-width: 168mm; }
h2 { font-size: 7.6pt; letter-spacing: 1.3px; text-transform: uppercase;
     color: %(head)s; margin: 10px 0 5px; padding-bottom: 3px;
     border-bottom: 1px solid %(head)s; break-after: avoid; }
.cards { display: flex; gap: 7px; }
.card { flex: 1; border: 1px solid %(bord)s; padding: 6px 8px 5px; }
.card .lbl { font-size: 6.8pt; letter-spacing: .8px; text-transform: uppercase;
             color: %(muted)s; }
.card .big { font-size: 20pt; font-weight: 300; line-height: 1.05; margin: 1px 0 0; }
.card .foot { font-size: 7pt; color: %(muted)s; margin-top: 3px; }
.d { font-size: 7.4pt; font-weight: 600; }
.d.up { color: %(up)s; } .d.down { color: %(down)s; } .d.flat { color: %(muted)s; }
table { border-collapse: collapse; width: 100%%; margin: 0; }
th { background: %(head)s; color: #fff; font-weight: 600; font-size: 6.9pt;
     padding: 4px 5px; border: 1px solid %(head)s; text-align: right;
     line-height: 1.2; }
th.l, td.l { text-align: left; }
td { border: 1px solid %(bord)s; padding: 3px 5px; text-align: right;
     font-size: 8pt; }
td.l { white-space: nowrap; }
tr.zebra td { background: %(zebra)s; }
tr.tot td { font-weight: 600; background: #eef2ec; }
td.sp { padding: 3px 5px; width: 68px; line-height: 0; }
tr { break-inside: avoid; }
.note { font-size: 7.2pt; color: %(muted)s; margin: 4px 0 0; }
.find { margin: 0 0 5px; font-size: 8.4pt; line-height: 1.4;
        break-inside: avoid; padding-left: 9px; text-indent: -9px; }
.finds { break-inside: avoid; }
.footer { margin-top: 10px; padding-top: 5px; border-top: 1px solid %(bord)s;
          font-size: 7pt; color: %(muted)s; }
""" % {"text": C_TEXT, "muted": C_MUTED, "head": C_HEAD, "up": C_UP,
       "down": C_DOWN, "bord": C_BORD, "zebra": C_ZEBRA}


def metric_table(rows: list[tuple], label: str, show_delta: bool) -> str:
    """rows: (подпись, текущие, прошлая неделя или None, спарклайн, итог?)"""
    dh = "<th>Отмены,<br>к пр.&nbsp;неделе</th>" if show_delta else ""
    dh2 = "<th>Задержки,<br>к пр.&nbsp;неделе</th>" if show_delta else ""
    h = ['<table><tr><th class="l">%s</th><th>Запла-<br>нировано</th>'
         '<th>Отме-<br>нено</th><th>Отменено,<br>%%</th>%s'
         '<th>1-2<br>часа</th><th>2-3<br>часа</th><th>больше<br>3 часов</th>'
         '<th>Задержано<br>от часа, %%</th>%s'
         '<th>Средняя<br>задержка,<br>мин</th>'
         '<th>Доля задержек<br>по дням</th></tr>' % (label, dh, dh2)]
    for i, (lab, r, p, sp, is_tot) in enumerate(rows):
        cls = ' class="tot"' if is_tot else (' class="zebra"' if i % 2 else "")
        dc = ("<td>%s</td>" % delta_html(r["cpct"], p["cpct"] if p else None)) \
            if show_delta else ""
        dd = ("<td>%s</td>" % delta_html(r["dpct"], p["dpct"] if p else None)) \
            if show_delta else ""
        h.append('<tr%s><td class="l">%s</td><td>%d</td><td>%d</td><td>%s%%</td>%s'
                 '<td>%d</td><td>%d</td><td>%d</td><td>%s%%</td>%s<td>%s</td>'
                 '<td class="sp">%s</td></tr>'
                 % (cls, lab, r["planned"], r["canceled"], num(r["cpct"]), dc,
                    r["b1"], r["b2"], r["b3"], num(r["dpct"]), dd,
                    num(r["avg"], 0), sp))
    h.append("</table>")
    return "".join(h)


def auto_text(facts: dict) -> dict:
    """Запасной разбор шаблоном, если файл с текстом не передали."""
    cur = facts["current"]
    tot = cur["total"]
    ap = cur["by_airport"]
    terms = cur["svo_terminals"]
    daily = cur["by_day"]
    worst = max(ap, key=lambda r: r["dpct"])
    intro = ("Запланировано %d вылетов, отменено %d, это %s%%. "
             "Задержано от часа %d рейсов из %d вылетевших, %s%%. "
             "%s: отмены %s%%, задержки %s%%."
             % (tot["planned"], tot["canceled"], num(tot["cpct"]),
                tot["dly"], tot["base"], num(tot["dpct"]),
                NAMES.get(worst["airport"], worst["airport"]),
                num(worst["cpct"]), num(worst["dpct"])))
    lines = ["%s: отменено %s%%, задержано %s%%, средняя задержка %s минут "
             "на рейс. Худшие цифры недели по обоим показателям."
             % (NAMES.get(worst["airport"], worst["airport"]),
                num(worst["cpct"]), num(worst["dpct"]), num(worst["avg"], 0))]
    if len(terms) > 1:
        tw = max(terms, key=lambda r: r["dpct"])
        tb = min(terms, key=lambda r: r["dpct"])
        lines.append("Шереметьево, терминал %s: задержано %s%%, средняя задержка "
                     "%s минут. В терминале %s задержано %s%% и %s минут."
                     % (tw["terminal"], num(tw["dpct"]), num(tw["avg"], 0),
                        tb["terminal"], num(tb["dpct"]), num(tb["avg"], 0)))
    if len(daily) > 1:
        lines.append("Задержки по суткам: %s."
                     % ", ".join("%s %s%%"
                                 % (date.fromisoformat(r["date"]).strftime("%d.%m"),
                                    num(r["dpct"])) for r in daily))
    if tot["dly"]:
        lines.append("Задержек свыше трёх часов %d, это %s%% всех задержанных "
                     "рейсов." % (tot["b3"], num(100.0 * tot["b3"] / tot["dly"])))
    return {"intro": intro, "blocks": [{"title": "Выводы", "lines": lines}]}


def render_html(facts: dict, analysis: dict) -> tuple[str, str, str]:
    cur = facts["current"]
    prev = facts.get("previous")
    tot = cur["total"]
    daily = cur["by_day"]
    days = [date.fromisoformat(r["date"]) for r in daily]
    prev_full = bool(prev) and len(prev["days"]) == 7
    show_delta = prev_full
    p_tot = prev["total"] if prev_full else None

    dseq = [d["dpct"] for d in daily]
    cseq = [d["cpct"] for d in daily]
    pseq = [float(d["planned"]) for d in daily]
    aseq = [d["avg"] for d in daily]

    ap_prev = {r["airport"]: r for r in prev["by_airport"]} if prev_full else {}
    zone_prev = {(r["airport"], r["zone"]): r
                 for r in prev["by_zone"]} if prev_full else {}
    term_prev = {(r["zone"], r["terminal"]): r
                 for r in prev["svo_terminals"]} if prev_full else {}

    ap_day: dict[str, list[float]] = {}
    for r in cur["by_airport_day"]:
        ap_day.setdefault(r["airport"], []).append(r["dpct"])

    ap = cur["by_airport"]
    terms = cur["svo_terminals"]
    zones = cur["by_zone"]

    def card(lbl, big, foot, values, prev_v, cur_v, digits=1, unit=" п.п."):
        d = delta_html(cur_v, prev_v, digits, unit) if prev_v is not None else ""
        return ('<div class="card"><div class="lbl">%s</div>'
                '<div class="big">%s</div>%s'
                '<div class="foot">%s</div>%s</div>'
                % (lbl, big, d, foot, spark(values)))

    p = ['<style>%s</style>' % CSS,
         '<div class="kicker">Еженедельный отчёт · вылеты из аэропортов Москвы</div>',
         '<h1>Задержки и отмены, %s</h1>' % period(days),
         '<div class="sub">Шереметьево, Внуково, Домодедово. '
         'Суток в отчёте: %d. Задержка считается от часа отклонения '
         'фактического отправления от плана.</div>' % len(daily),
         '<div class="rule"></div>',
         '<div class="intro">%s</div>' % analysis["intro"]]

    p.append('<div class="cards">')
    p.append(card("Запланировано", "%d" % tot["planned"], "по дням, рейсов",
                  pseq, None if not p_tot else float(p_tot["planned"]),
                  float(tot["planned"]), 0, ""))
    p.append(card("Отменено", num(tot["cpct"]) + "%",
                  "%d рейсов, доля по дням" % tot["canceled"], cseq,
                  None if not p_tot else p_tot["cpct"], tot["cpct"]))
    p.append(card("Задержано от часа", num(tot["dpct"]) + "%",
                  "%d рейсов, доля по дням" % tot["dly"], dseq,
                  None if not p_tot else p_tot["dpct"], tot["dpct"]))
    p.append(card("Средняя задержка", num(tot["avg"], 0) + " мин",
                  "на вылетевший рейс, по дням", aseq,
                  None if not p_tot else p_tot["avg"], tot["avg"], 0, " мин"))
    p.append("</div>")

    p.append("<h2>По аэропортам</h2>")
    rows = [("Все три аэропорта", tot, p_tot, spark(dseq, h=15), True)]
    for r in ap:
        rows.append((NAMES.get(r["airport"], r["airport"]), r,
                     ap_prev.get(r["airport"]),
                     spark(ap_day.get(r["airport"], []), h=15), False))
    p.append(metric_table(rows, "Аэропорт", show_delta))

    p.append("<h2>По зонам</h2>")
    rows = []
    for r in zones:
        rows.append(("%s, %s" % (NAMES.get(r["airport"], r["airport"]), r["zone"]),
                     r, zone_prev.get((r["airport"], r["zone"])), "", False))
    p.append(metric_table(rows, "Аэропорт и зона", show_delta))
    unk = cur.get("unknown_planned") or 0
    if unk:
        p.append('<p class="note">Вне разреза по зонам %d рейсов без пункта '
                 'назначения, в итог по аэропортам они входят.</p>' % unk)

    p.append("<h2>Шереметьево по терминалам</h2>")
    rows = []
    for r in terms:
        rows.append(("%s, терминал %s" % (r["zone"], r["terminal"]), r,
                     term_prev.get((r["zone"], r["terminal"])), "", False))
    p.append(metric_table(rows, "Зона и терминал", show_delta))

    p.append("<h2>По суткам</h2>")
    h = ['<table><tr><th class="l">Дата</th><th>Запланировано</th>'
         '<th>Отменено</th><th>Отменено, %</th><th>Вылетело</th>'
         '<th>1-2 часа</th><th>2-3 часа</th><th>больше 3 часов</th>'
         '<th>Задержано от часа, %</th><th>Средняя задержка, мин</th></tr>']
    for i, r in enumerate(daily):
        d = date.fromisoformat(r["date"])
        h.append('<tr%s><td class="l">%s, %s</td><td>%d</td><td>%d</td><td>%s%%</td>'
                 '<td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%s%%</td>'
                 '<td>%s</td></tr>'
                 % (' class="zebra"' if i % 2 else "", WD[d.weekday()],
                    d.strftime("%d.%m"), r["planned"], r["canceled"],
                    num(r["cpct"]), r["base"], r["b1"], r["b2"], r["b3"],
                    num(r["dpct"]), num(r["avg"], 0)))
    h.append("</table>")
    p.append("".join(h))

    for block in analysis.get("blocks") or []:
        lines = [x for x in (block.get("lines") or []) if x and x.strip()]
        if not lines:
            continue
        p.append('<h2>%s</h2><div class="finds">' % block["title"])
        for line in lines:
            p.append('<p class="find">· %s</p>' % line)
        p.append('</div>')

    dme = next((r for r in ap if r["airport"] == "DME"), None)
    worst = max(ap, key=lambda r: r["dpct"])
    dme_note = ""
    if dme and dme["dpct"] < 10 and worst["dpct"] > 20:
        dme_note = ("Домодедово: %s%% задержек. Источник обновляет время вылета "
                    "только у опоздавших рейсов и обрезает крупные опоздания, за "
                    "июнь-август максимум 111 минут на 8928 рейсов. Долю по "
                    "Домодедову с двумя другими аэропортами напрямую не "
                    "сравниваем. " % num(dme["dpct"]))

    p.append('<div class="footer">%sИсточник: сбор бота по расписанию вылетов '
             'AeroDataBox, те же данные, что в ежедневной рассылке. '
             'Рейсы без факта отправления в базу для процента задержек не '
             'входят, за период таких %d. Отчёт собран %s.</div>'
             % (dme_note, tot["no_fact"], date.today().strftime("%d.%m.%Y")))

    html = "".join(p)
    subject = ("Вылеты за неделю %s: отменено %s%%, задержано от часа %s%%"
               % (period(days), num(tot["cpct"]), num(tot["dpct"])))
    body = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;'
            'color:#1a1a1a"><p>Вылеты из Шереметьева, Внукова и Домодедова '
            'за %s.</p><p>%s</p><p>Дашборд в приложенном PDF.</p></div>'
            % (period(days), analysis["intro"]))
    check_slop(html + " " + body + " " + subject)
    return html, body, subject


def render(html: str, pdf_path: str) -> str:
    from weasyprint import HTML
    HTML(string=html).write_pdf(pdf_path)
    return pdf_path


def main() -> int:
    ws = (os.environ.get("WEEK_END") or "").strip()
    end = date.fromisoformat(ws) if ws else latest_closed_week()
    facts = collect_facts(end)

    out_facts = (os.environ.get("WEEK_FACTS") or "").strip()
    if out_facts:
        with open(out_facts, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False, indent=1)
        log.info("Факты за %s (%s) -> %s", facts["period"],
                 ", ".join(facts["current"]["days"]), out_facts)
        return 0

    an_path = (os.environ.get("WEEK_ANALYSIS") or "").strip()
    if an_path:
        analysis = json.loads(open(an_path, encoding="utf-8").read())
        if not analysis.get("intro"):
            raise SystemExit("в файле разбора нет intro")
    else:
        analysis = auto_text(facts)

    html, body, subject = render_html(facts, analysis)
    pdf = "/tmp/weekly_%s.pdf" % end.isoformat()
    with open("/tmp/weekly.html", "w", encoding="utf-8") as f:
        f.write(html)
    render(html, pdf)
    with open("/tmp/weekly_body.html", "w", encoding="utf-8") as f:
        f.write(body)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("subject=%s\n" % subject)
            f.write("pdf=%s\n" % pdf)
            f.write("week_end=%s\n" % end.isoformat())
    log.info("Тема: %s", subject)
    log.info("PDF: %s", pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Недельный дашборд по вылетам: PDF в палитре «Контраст» плюс тело письма.

Берёт data/ops_daily/<дата>.json за отчётную неделю и за предыдущую,
считает всё в DuckDB, складывает:
    /tmp/weekly_<конец>.pdf   — дашборд A4 портрет
    /tmp/weekly_body.html     — короткое тело письма
Тема письма уходит в GITHUB_OUTPUT, если он есть.

Отчётная неделя это понедельник-воскресенье. По умолчанию берётся та,
которая полностью закрыта сводками. Можно задать руками:
    WEEK_END=2026-09-13 python -m src.weekly_report

Сравнение с предыдущей неделей показывается только если она закрыта
теми же семью сводками текущей схемы. Если базы нет, столбцы динамики
просто не рисуются.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import duckdb

from src.ops_report import OPS_DIR, SCHEMA
from src.utils import get_logger

log = get_logger("weekly")

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
NAMES = {"SVO": "Шереметьево", "VKO": "Внуково", "DME": "Домодедово"}
WD = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Палитра «Контраст»
C_TEXT = "#1a1a1a"
C_MUTED = "#6b6b6b"
C_HEAD = "#1b5e20"
C_UP = "#c62828"      # рост задержек и отмен это ухудшение
C_DOWN = "#1b7a3d"
C_BORD = "#dddddd"
C_ZEBRA = "#f5f7f4"
C_AXIS = "#cccccc"


# ---------------------------------------------------------------- данные

def read_rows(days: list[date]) -> list[dict]:
    out = []
    for d in days:
        p = OPS_DIR / ("%s.json" % d.isoformat())
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        if j.get("schema") != SCHEMA:
            continue
        for r in j["rows"]:
            r = dict(r)
            r["date"] = j["date"]
            out.append(r)
    return out


def week_days(end: date) -> list[date]:
    return [end - timedelta(days=i) for i in range(6, -1, -1)]


def latest_closed_week() -> date:
    """Воскресенье последней недели, у которой есть все семь сводок схемы 2."""
    d = date.today()
    d -= timedelta(days=(d.weekday() + 1) % 7)  # ближайшее прошедшее воскресенье
    for _ in range(8):
        if all((OPS_DIR / ("%s.json" % x.isoformat())).exists() for x in week_days(d)):
            return d
        d -= timedelta(days=7)
    return d


class Agg:
    """Все разрезы недели. Считает DuckDB, руками ничего не складываем."""

    def __init__(self, rows: list[dict]):
        self.ok = bool(rows)
        self.con = duckdb.connect()
        path = "/tmp/wk_rows_%d.json" % id(self)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows or [{}], f, ensure_ascii=False)
        if rows:
            self.con.execute(
                "CREATE TABLE t AS SELECT * FROM read_json_auto('%s')" % path)

    M = ("sum(planned) AS planned, sum(canceled) AS canceled, "
         "sum(departed) - sum(no_fact) AS base, sum(delayed_total) AS dly, "
         "sum(delay_60_120) AS b1, sum(delay_120_180) AS b2, "
         "sum(delay_180_plus) AS b3, sum(delay_min_total) AS dmin, "
         "sum(no_fact) AS no_fact, sum(diverted) AS diverted")

    def _q(self, sql: str) -> list[dict]:
        if not self.ok:
            return []
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["cpct"] = 100.0 * d["canceled"] / d["planned"] if d["planned"] else 0.0
            d["dpct"] = 100.0 * d["dly"] / d["base"] if d["base"] else 0.0
            d["avg"] = d["dmin"] / d["base"] if d["base"] else 0.0
            out.append(d)
        return out

    def total(self) -> dict:
        r = self._q("SELECT %s FROM t" % self.M)
        return r[0] if r else {}

    def by_day(self) -> list[dict]:
        return self._q("SELECT CAST(date AS VARCHAR) AS date, %s FROM t GROUP BY 1 ORDER BY 1" % self.M)

    def by_airport(self) -> list[dict]:
        return self._q("SELECT airport, %s FROM t GROUP BY 1 ORDER BY 1" % self.M)

    def by_airport_day(self) -> list[dict]:
        return self._q("SELECT airport, CAST(date AS VARCHAR) AS date, %s FROM t GROUP BY 1,2 ORDER BY 1,2"
                       % self.M)

    def by_zone(self) -> list[dict]:
        return self._q("SELECT airport, zone, %s FROM t WHERE zone <> '?' "
                       "GROUP BY 1,2 ORDER BY 1,2" % self.M)

    def svo_terminals(self) -> list[dict]:
        return self._q("SELECT zone, terminal, %s FROM t WHERE airport = 'SVO' "
                       "AND zone <> '?' GROUP BY 1,2 ORDER BY 1,2" % self.M)

    def by_zone_day(self) -> list[dict]:
        return self._q("SELECT airport, zone, CAST(date AS VARCHAR) AS date, %s "
                       "FROM t WHERE zone <> '?' GROUP BY 1,2,3 ORDER BY 1,2,3"
                       % self.M)

    def svo_terminals_day(self) -> list[dict]:
        return self._q("SELECT zone, terminal, CAST(date AS VARCHAR) AS date, %s "
                       "FROM t WHERE airport = 'SVO' AND zone <> '?' "
                       "GROUP BY 1,2,3 ORDER BY 1,2,3" % self.M)

    def unknown(self) -> int:
        r = self._q("SELECT %s FROM t WHERE zone = '?'" % self.M)
        return r[0]["planned"] if r else 0


# ---------------------------------------------------------------- вёрстка

def human(d: date) -> str:
    return "%d %s" % (d.day, MONTHS[d.month - 1])


def period(days: list[date]) -> str:
    a, b = days[0], days[-1]
    if a.month == b.month:
        return "%d-%d %s %d" % (a.day, b.day, MONTHS[a.month - 1], b.year)
    return "%s - %s %d" % (human(a), human(b), b.year)


def num(v: float, digits: int = 1) -> str:
    return ("%%.%df" % digits % v).replace(".", ",")


def sign(v: float, digits: int = 1, unit: str = "") -> str:
    s = "+" if v > 0 else ("−" if v < 0 else "")
    return "%s%s%s" % (s, num(abs(v), digits), unit)


def delta_html(cur: float, prev, digits=1, unit=" п.п.", worse_up=True) -> str:
    if prev is None:
        return ""
    d = cur - prev
    if abs(d) < 0.05 and digits <= 1:
        return '<span class="d flat">0%s</span>' % unit
    bad = (d > 0) if worse_up else (d < 0)
    return '<span class="d %s">%s</span>' % ("up" if bad else "down",
                                             sign(d, digits, unit))


def spark(values: list[float], w: int = 150, h: int = 26) -> str:
    """Спарклайн по дням. Ось снизу, значения нормируются на максимум."""
    if not values:
        return ""
    top = max(values) or 1.0
    n = len(values)
    step = w / max(n - 1, 1)
    pts = " ".join("%.1f,%.1f" % (i * step, h - 2 - (v / top) * (h - 6))
                   for i, v in enumerate(values))
    last_x = (n - 1) * step
    last_y = h - 2 - (values[-1] / top) * (h - 6)
    return ('<svg class="sp" viewBox="0 0 %d %d" preserveAspectRatio="none" '
            'style="width:100%%;height:%dpx;display:block">'
            '<line x1="0" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1"/>'
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6"/>'
            '<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s"/></svg>'
            % (w, h, h, h - 1, w, h - 1, C_AXIS, pts, C_HEAD,
               last_x, last_y, C_HEAD))


# Формулировки в отчёте зашиты в код, ИИ в GitHub Actions не работает.
# Этот список ловит их, если кто-то поправит тексты и занесёт нейрослоп.
SLOP = ("—", "важно отметить", "стоит подчеркнуть", "следует учитывать",
        "в современном мире", "ключевым фактором", "играет важную роль",
        "представляет собой", "не только", "комплексный подход", "синергия",
        "в свою очередь", "таким образом", "данный", "данная", "данные о том",
        "осуществляется", "производится", "подводя итог", "в целом",
        "при этом", "стоит отметить", "разогнался", "провалился",
        "картина обратная", "остаётся ядром")


def check_slop(text: str) -> None:
    """Проверка текста отчёта на длинное тире и обороты из списка anti-slop."""
    import re as _re
    plain = _re.sub(r"<style.*?</style>", " ", text, flags=_re.S)
    plain = _re.sub(r"<[^>]+>", " ", plain).lower()
    hits = [w for w in SLOP if w in plain]
    if hits:
        raise SystemExit("в тексте отчёта нейрослоп: %s" % ", ".join(hits))


CSS = """
@page { size: A4 portrait; margin: 13mm 12mm 11mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI","DejaVu Sans",sans-serif; font-size: 8.6pt;
       color: %(text)s; margin: 0; }
.kicker { font-size: 7pt; letter-spacing: 1.6px; color: %(muted)s;
          text-transform: uppercase; }
h1 { font-size: 20pt; font-weight: 300; margin: 3px 0 0; }
.sub { font-size: 8.4pt; color: %(muted)s; margin-top: 2px; }
.rule { height: 2px; background: %(head)s; margin: 8px 0 10px; }
.intro { font-size: 9pt; line-height: 1.45; margin: 0 0 10px; max-width: 168mm; }
h2 { font-size: 7.6pt; letter-spacing: 1.3px; text-transform: uppercase;
     color: %(head)s; margin: 10px 0 5px; padding-bottom: 3px;
     border-bottom: 1px solid %(head)s; break-after: avoid; }
.cards { display: flex; gap: 7px; }
.card { flex: 1; border: 1px solid %(bord)s; padding: 6px 8px 5px; }
.card .lbl { font-size: 6.8pt; letter-spacing: .8px; text-transform: uppercase;
             color: %(muted)s; }
.card .big { font-size: 20pt; font-weight: 300; line-height: 1.05; margin: 1px 0 0; }
.card .foot { font-size: 7pt; color: %(muted)s; margin-top: 3px; }
.d { font-size: 7.4pt; font-weight: 600; }
.d.up { color: %(up)s; } .d.down { color: %(down)s; } .d.flat { color: %(muted)s; }
table { border-collapse: collapse; width: 100%%; margin: 0; }
th { background: %(head)s; color: #fff; font-weight: 600; font-size: 6.9pt;
     padding: 4px 5px; border: 1px solid %(head)s; text-align: right;
     line-height: 1.2; }
th.l, td.l { text-align: left; }
td { border: 1px solid %(bord)s; padding: 3px 5px; text-align: right;
     font-size: 8pt; }
td.l { white-space: nowrap; }
tr.zebra td { background: %(zebra)s; }
tr.tot td { font-weight: 600; background: #eef2ec; }
td.sp { padding: 3px 5px; width: 68px; line-height: 0; }
.note { font-size: 7.2pt; color: %(muted)s; margin: 4px 0 0; }
.cols { display: flex; gap: 12px; margin-top: 4px; }
.cols .c { flex: 1; }
.find { margin: 0 0 5px; font-size: 8.4pt; line-height: 1.4;
        break-inside: avoid; }
.finds { break-inside: avoid; }
tr { break-inside: avoid; }
.find b { color: %(head)s; }
.footer { margin-top: 10px; padding-top: 5px; border-top: 1px solid %(bord)s;
          font-size: 7pt; color: %(muted)s; }
""" % {"text": C_TEXT, "muted": C_MUTED, "head": C_HEAD, "up": C_UP,
       "down": C_DOWN, "bord": C_BORD, "zebra": C_ZEBRA}


def metric_table(rows: list[tuple], base: dict, label: str, show_delta: bool) -> str:
    """rows: (подпись, текущие, прошлая неделя или None, спарклайн или '', итог?)"""
    dh = "<th>Отмены,<br>к пр.&nbsp;неделе</th>" if show_delta else ""
    dh2 = "<th>Задержки,<br>к пр.&nbsp;неделе</th>" if show_delta else ""
    h = ['<table><tr><th class="l">%s</th><th>Запла-<br>нировано</th>'
         '<th>Отме-<br>нено</th><th>Отменено,<br>%%</th>%s'
         '<th>1-2<br>часа</th><th>2-3<br>часа</th><th>больше<br>3 часов</th>'
         '<th>Задержано<br>от часа, %%</th>%s'
         '<th>Средняя<br>задержка,<br>мин</th>'
         '<th>Доля задержек<br>по дням</th></tr>' % (label, dh, dh2)]
    for i, (lab, r, p, sp, is_tot) in enumerate(rows):
        cls = ' class="tot"' if is_tot else (' class="zebra"' if i % 2 else "")
        dc = ("<td>%s</td>" % delta_html(r["cpct"], p["cpct"] if p else None)) \
            if show_delta else ""
        dd = ("<td>%s</td>" % delta_html(r["dpct"], p["dpct"] if p else None)) \
            if show_delta else ""
        h.append('<tr%s><td class="l">%s</td><td>%d</td><td>%d</td><td>%s%%</td>%s'
                 '<td>%d</td><td>%d</td><td>%d</td><td>%s%%</td>%s<td>%s</td>'
                 '<td class="sp">%s</td></tr>'
                 % (cls, lab, r["planned"], r["canceled"], num(r["cpct"]), dc,
                    r["b1"], r["b2"], r["b3"], num(r["dpct"]), dd,
                    num(r["avg"], 0), sp))
    h.append("</table>")
    return "".join(h)


def build(end: date) -> tuple[str, str, str]:
    days = week_days(end)
    prev_days = week_days(end - timedelta(days=7))
    cur = Agg(read_rows(days))
    if not cur.ok:
        raise SystemExit("нет ни одной сводки схемы %d за %s" % (SCHEMA, period(days)))
    prev = Agg(read_rows(prev_days))
    prev_full = len(prev.by_day()) == 7
    show_delta = prev_full

    tot = cur.total()
    p_tot = prev.total() if prev_full else None
    daily = cur.by_day()
    # Подпись периода строим по суткам, которые реально есть в базе. На
    # первой неделе внедрения их может быть меньше семи.
    days = [date.fromisoformat(r["date"]) for r in daily]
    dseq = [d["dpct"] for d in daily]
    cseq = [d["cpct"] for d in daily]
    pseq = [float(d["planned"]) for d in daily]
    aseq = [d["avg"] for d in daily]

    ap_prev = {r["airport"]: r for r in prev.by_airport()} if prev_full else {}
    ap_day: dict[str, list[float]] = {}
    for r in cur.by_airport_day():
        ap_day.setdefault(r["airport"], []).append(r["dpct"])
    zone_prev = {(r["airport"], r["zone"]): r for r in prev.by_zone()} if prev_full else {}
    zone_day: dict[tuple, list[float]] = {}
    for r in cur.by_zone_day():
        zone_day.setdefault((r["airport"], r["zone"]), []).append(r["dpct"])
    term_day: dict[tuple, list[float]] = {}
    for r in cur.svo_terminals_day():
        term_day.setdefault((r["zone"], r["terminal"]), []).append(r["dpct"])
    term_prev = {(r["zone"], r["terminal"]): r
                 for r in prev.svo_terminals()} if prev_full else {}

    ap = cur.by_airport()
    worst_d = max(ap, key=lambda r: r["dpct"])
    terms = cur.svo_terminals()
    zones = cur.by_zone()

    def card(lbl, big, foot, values, prev_v=None, digits=1, unit=" п.п."):
        d = delta_html(big if isinstance(big, float) else 0.0, prev_v, digits, unit) \
            if prev_v is not None else ""
        return ('<div class="card"><div class="lbl">%s</div>'
                '<div class="big">%s</div>%s'
                '<div class="foot">%s</div>%s</div>'
                % (lbl, big if isinstance(big, str) else num(big), d, foot,
                   spark(values)))

    p = ['<style>%s</style>' % CSS,
         '<div class="kicker">Еженедельный отчёт · вылеты из аэропортов Москвы</div>',
         '<h1>Задержки и отмены, %s</h1>' % period(days),
         '<div class="sub">Шереметьево, Внуково, Домодедово. '
         'Суток в отчёте: %d. Задержка считается от часа отклонения '
         'фактического отправления от плана.</div>' % len(daily),
         '<div class="rule"></div>']

    intro = ("Запланировано %d вылетов, отменено %d, это %s%%. "
             "Задержано от часа %d рейсов из %d вылетевших, %s%%. "
             "%s: отмены %s%%, задержки %s%%."
             % (tot["planned"], tot["canceled"], num(tot["cpct"]),
                tot["dly"], tot["base"], num(tot["dpct"]),
                NAMES.get(worst_d["airport"], worst_d["airport"]),
                num(worst_d["cpct"]), num(worst_d["dpct"])))
    p.append('<div class="intro">%s</div>' % intro)

    p.append('<div class="cards">')
    p.append(card("Запланировано", "%d" % tot["planned"],
                  "по дням, рейсов", pseq,
                  None if not p_tot else float(p_tot["planned"])))
    p.append(card("Отменено", num(tot["cpct"]) + "%",
                  "%d рейсов, доля по дням" % tot["canceled"], cseq,
                  None if not p_tot else p_tot["cpct"]))
    p.append(card("Задержано от часа", num(tot["dpct"]) + "%",
                  "%d рейсов, доля по дням" % tot["dly"], dseq,
                  None if not p_tot else p_tot["dpct"]))
    p.append(card("Средняя задержка", num(tot["avg"], 0) + " мин",
                  "на вылетевший рейс, по дням", aseq,
                  None if not p_tot else p_tot["avg"], 0, " мин"))
    p.append("</div>")

    p.append("<h2>По аэропортам</h2>")
    rows = [("Все три аэропорта", tot, p_tot, spark(dseq, h=15), True)]
    for r in ap:
        rows.append((NAMES.get(r["airport"], r["airport"]), r,
                     ap_prev.get(r["airport"]), spark(ap_day.get(r["airport"], []), h=15),
                     False))
    p.append(metric_table(rows, tot, "Аэропорт", show_delta))

    p.append("<h2>По зонам</h2>")
    rows = []
    for r in zones:
        rows.append(("%s, %s" % (NAMES.get(r["airport"], r["airport"]), r["zone"]),
                     r, zone_prev.get((r["airport"], r["zone"])),
                     spark(zone_day.get((r["airport"], r["zone"]), []), h=15), False))
    p.append(metric_table(rows, tot, "Аэропорт и зона", show_delta))
    unk = cur.unknown()
    if unk:
        p.append('<p class="note">Вне разреза по зонам %d рейсов без пункта '
                 'назначения, в итог по аэропортам они входят.</p>' % unk)

    p.append("<h2>Шереметьево по терминалам</h2>")
    rows = []
    for r in terms:
        rows.append(("%s, терминал %s" % (r["zone"], r["terminal"]), r,
                     term_prev.get((r["zone"], r["terminal"])),
                     spark(term_day.get((r["zone"], r["terminal"]), []), h=15), False))
    p.append(metric_table(rows, tot, "Зона и терминал", show_delta))

    p.append("<h2>По суткам</h2>")
    h = ['<table><tr><th class="l">Дата</th><th>Запланировано</th>'
         '<th>Отменено</th><th>Отменено, %</th><th>Вылетело</th>'
         '<th>1-2 часа</th><th>2-3 часа</th><th>больше 3 часов</th>'
         '<th>Задержано от часа, %</th><th>Средняя задержка, мин</th></tr>']
    for i, r in enumerate(daily):
        d = date.fromisoformat(r["date"])
        h.append('<tr%s><td class="l">%s, %s</td><td>%d</td><td>%d</td><td>%s%%</td>'
                 '<td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%s%%</td>'
                 '<td>%s</td></tr>'
                 % (' class="zebra"' if i % 2 else "", WD[d.weekday()],
                    d.strftime("%d.%m"), r["planned"], r["canceled"],
                    num(r["cpct"]), r["base"], r["b1"], r["b2"], r["b3"],
                    num(r["dpct"]), num(r["avg"], 0)))
    h.append("</table>")
    p.append("".join(h))

    p.append('<h2>Выводы</h2><div class="finds">')
    finds = []
    finds.append("%s: отменено %s%%, задержано %s%%, средняя задержка %s минут "
                 "на рейс. Худшие цифры недели по обоим показателям."
                 % (NAMES.get(worst_d["airport"], worst_d["airport"]),
                    num(worst_d["cpct"]), num(worst_d["dpct"]),
                    num(worst_d["avg"], 0)))
    if len(terms) > 1:
        t_worst = max(terms, key=lambda r: r["dpct"])
        t_best = min(terms, key=lambda r: r["dpct"])
        finds.append("Шереметьево, терминал %s: задержано %s%%, средняя "
                     "задержка %s минут. В терминале %s задержано %s%% и %s "
                     "минут. Разрыв внутри одного аэропорта."
                     % (t_worst["terminal"], num(t_worst["dpct"]),
                        num(t_worst["avg"], 0), t_best["terminal"],
                        num(t_best["dpct"]), num(t_best["avg"], 0)))
    if len(daily) > 1:
        finds.append("Задержки по суткам: %s."
                     % ", ".join("%s %s%%" % (date.fromisoformat(r["date"]).strftime("%d.%m"),
                                              num(r["dpct"])) for r in daily))
        finds.append("Отмены по суткам: %s."
                     % ", ".join("%s %s%%" % (date.fromisoformat(r["date"]).strftime("%d.%m"),
                                              num(r["cpct"])) for r in daily))
    heavy = tot["b3"]
    if tot["dly"]:
        finds.append("Задержек свыше трёх часов %d, это %s%% всех задержанных "
                     "рейсов." % (heavy, num(100.0 * heavy / tot["dly"])))
    for f in finds:
        p.append('<p class="find">%s</p>' % f)
    p.append('</div>')

    # Методическая оговорка по Домодедову идёт в подвал мелким шрифтом, а не
    # в выводы: цифра там занижена не по операционным причинам.
    dme = next((r for r in ap if r["airport"] == "DME"), None)
    dme_note = ""
    if dme and dme["dpct"] < 10 and worst_d["dpct"] > 20:
        dme_note = ("Домодедово: %s%% задержек. Источник обновляет время вылета "
                    "только у опоздавших рейсов и обрезает крупные опоздания, за "
                    "июнь-август максимум 111 минут на 8928 рейсов. Долю по "
                    "Домодедову с двумя другими аэропортами напрямую не "
                    "сравниваем. " % num(dme["dpct"]))

    p.append('<div class="footer">%sИсточник: сбор бота по расписанию вылетов '
             'AeroDataBox, те же данные, что в ежедневной рассылке. '
             'Рейсы без факта отправления в базу для процента задержек не '
             'входят, за период таких %d. Отчёт собран %s.</div>'
             % (dme_note, tot["no_fact"], date.today().strftime("%d.%m.%Y")))

    html = "".join(p)
    subject = ("Вылеты за неделю %s: отменено %s%%, задержано от часа %s%%"
               % (period(days), num(tot["cpct"]), num(tot["dpct"])))
    body = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;'
            'color:#1a1a1a"><p>Вылеты из Шереметьева, Внукова и Домодедова '
            'за %s.</p><p>%s</p><p>Дашборд в приложенном PDF.</p></div>'
            % (period(days), intro))
    check_slop(html + " " + body + " " + subject)
    return html, body, subject


def render(html: str, pdf_path: str) -> str:
    from weasyprint import HTML
    HTML(string=html).write_pdf(pdf_path)
    return pdf_path


def main() -> int:
    ws = (os.environ.get("WEEK_END") or "").strip()
    end = date.fromisoformat(ws) if ws else latest_closed_week()
    html, body, subject = build(end)
    pdf = "/tmp/weekly_%s.pdf" % end.isoformat()
    with open("/tmp/weekly.html", "w", encoding="utf-8") as f:
        f.write(html)
    render(html, pdf)
    with open("/tmp/weekly_body.html", "w", encoding="utf-8") as f:
        f.write(body)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("subject=%s\n" % subject)
            f.write("pdf=%s\n" % pdf)
            f.write("week_end=%s\n" % end.isoformat())
    log.info("Тема: %s", subject)
    log.info("PDF: %s", pdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
