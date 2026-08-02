"""Отчёт загрузки гейтов: ДВА базиса сравнения, без мешанины источников.

Проблема старой версии: доля точки считалась как «гейты точки / весь терминал»
из единой склейки история(коллеги) + бот. У коллег в терминале учтены только
свои гейты, у бота — все. При переходе июнь(коллеги)->июль(бот) знаменатель
скачком менялся, и доли рушились. Плюс склейка не дедуплицировалась (пересечение
конца июня считалось дважды).

Новая версия считает КАЖДУЮ метрику на одном согласованном базисе:

  1. «Весь аэропорт» — только данные бота (он собирает все гейты). Доступно с
     конца мая 2026. Доля точки = гейты точки / все рейсы терминала.

  2. «Наши гейты» (как у коллег) — только гейты, которые ведут коллеги
     (набор TRACKED). Источник по дате: где есть история коллег — берём её
     (июнь и раньше), дальше — бот. Пересечение дат дедуплицируется в пользу
     коллег. Доля точки = гейты точки / наши гейты терминала.

Так июнь остаётся «как у коллег», а июль сравним с июнем внутри каждого базиса.

Запуск:
    python -m src.report --to 2026-07
"""
from __future__ import annotations

import argparse
import re
from datetime import date

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---- точки Винегрет (аэропорт, терминал, спецификация гейтов) --------------
POINTS = [
    {"point": "АБ460",      "airport": "VKO", "terminal": "A", "gates": "11-12"},
    {"point": "Кофеин ВВЛ", "airport": "VKO", "terminal": "A", "gates": "13"},
    {"point": "Бад",        "airport": "VKO", "terminal": "A", "gates": "15-16"},
    {"point": "АБ99",       "airport": "VKO", "terminal": "A", "gates": "21-22"},
    {"point": "Ачарули",    "airport": "VKO", "terminal": "A", "gates": "23-24"},
    {"point": "Бир3/Кофеин МВЛ", "airport": "VKO", "terminal": "A", "gates": "24-25"},
    {"point": "Баттерфляй", "airport": "DME", "terminal": "C", "gates": "C5-C7"},
    {"point": "АБ131",      "airport": "DME", "terminal": "D", "gates": "D3-D8"},
    {"point": "Гурмэ Т2",   "airport": "DME", "terminal": "E", "gates": "E13,E14"},
    {"point": "Гурмэ В",    "airport": "SVO", "terminal": "B", "gates": "117-121"},
    {"point": "АБ600",      "airport": "SVO", "terminal": "D", "gates": "D14-D17"},
]

# ---- гейты, которые ведут коллеги (канонические ключи) ---------------------
# Знаменатель базиса «наши гейты». Меняются редко; при добавлении коллегами
# нового гейта — дописать сюда.
TRACKED = {
    "VKO": {"11", "12", "13", "15", "16", "18", "19", "20",
            "21", "22", "23", "24", "25"},
    "DME": {"C3", "C4", "C5", "C6", "C7", "C8", "C12", "C13", "C14", "C15",
            "C16", "C17", "C18", "C19", "D3", "D4", "D5", "D6", "D7", "D8",
            "D9", "D10", "D11", "D13", "D14", "D16", "D17",
            "E8", "E9", "E10", "E12", "E13", "E14"},
    "SVO": {"117", "118", "119", "120", "121",
            "124", "125", "126", "127", "128", "129", "130", "131", "132",
            "133", "134", "135", "136", "137", "138", "139", "140", "141",
            "142", "143", "144", "145", "146", "D14", "D15", "D16", "D17"},
}


def canon_gate(airport: str, terminal, gate) -> str | None:
    """Единый ключ гейта для бота и коллег.
    VKO: только цифры (11A и 11 -> '11', '08' -> '8').
    SVO: гейты терминала D 14-17 и 'D14' -> 'D14'; прочие цифры -> число;
         диапазонные ярлыки коллег ('124-129,134-139') остаются как есть.
    DME: 'C5','D3','E13' как есть (uppercase)."""
    if gate is None:
        return None
    g = str(gate).strip().upper()
    if g in ("", "—", "-", "NONE", "NAN"):
        return None
    t = str(terminal or "").strip().upper()
    if airport == "VKO":
        d = "".join(ch for ch in g if ch.isdigit())
        return str(int(d)) if d else None
    if airport == "SVO":
        m = re.match(r"^D0*(\d+)$", g)
        if m:
            return "D" + m.group(1)
        if g.isdigit():
            n = int(g)
            if t == "D" and n in (14, 15, 16, 17):
                return "D" + str(n)
            return str(n)
        return g
    return g


def _canon_num(prefix: str, num: int) -> str:
    return f"{prefix.upper()}{num}"


def expand_spec(spec: str) -> set[str]:
    """Спецификацию точки ('11-12','C5-C7','D14-D17','E13,E14','13') развернуть
    в множество канонических ключей."""
    out: set[str] = set()
    for tok in str(spec).split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            ma = re.match(r"^([A-ZА-Я]*)0*(\d+)[A-ZА-Я]?$", a)
            mb = re.match(r"^([A-ZА-Я]*)0*(\d+)[A-ZА-Я]?$", b)
            if not ma or not mb:
                continue
            prefix = ma.group(1) or mb.group(1)
            lo, hi = sorted([int(ma.group(2)), int(mb.group(2))])
            for n in range(lo, hi + 1):
                out.add(_canon_num(prefix, n))
        else:
            m = re.match(r"^([A-ZА-Я]*)0*(\d+)[A-ZА-Я]?$", tok)
            if m:
                out.add(_canon_num(m.group(1), int(m.group(2))))
    return out


POINT_GATES = {p["point"]: expand_spec(p["gates"]) for p in POINTS}

# ---- стили -----------------------------------------------------------------
FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F4E78")
HDR_FONT = Font(FONT, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(FONT, bold=True, size=13, color="1F4E78")
BASE = Font(FONT, size=10)
BOLD = Font(FONT, bold=True, size=10)
GREY = Font(FONT, size=9, color="808080")
GREEN = Font(FONT, size=10, bold=True, color="006100")
RED = Font(FONT, size=10, bold=True, color="9C0006")
THIN = Side(style="thin", color="D0D0D0")
BORDER = Border(THIN, THIN, THIN, THIN)
CENTER = Alignment(horizontal="center", vertical="center")


def _prep(df: pd.DataFrame, dfrom, dto) -> pd.DataFrame:
    d = df.copy()
    d["flight_date"] = pd.to_datetime(d["flight_date"], errors="coerce")
    d = d[d["flight_date"].notna()].copy()
    d["ym"] = d["flight_date"].dt.strftime("%Y-%m")
    if dfrom:
        d = d[d["ym"] >= dfrom]
    if dto:
        d = d[d["ym"] <= dto]
    d["airport"] = d["airport"].astype(str).str.strip()
    d["terminal"] = d["terminal"].fillna("").astype(str).str.strip()
    if "src" not in d.columns:
        d["src"] = "daily"
    d["cgate"] = [canon_gate(a, t, g)
                  for a, t, g in zip(d["airport"], d["terminal"], d["gate"])]
    d["daykey"] = d["airport"] + "|" + d["flight_date"].dt.strftime("%Y-%m-%d")
    return d


def _whole(d: pd.DataFrame) -> pd.DataFrame:
    """Базис «весь аэропорт»: только бот."""
    return d[d["src"] == "daily"].copy()


def _tracked(d: pd.DataFrame) -> pd.DataFrame:
    """Базис «наши гейты»: коллеги где есть (по дате), иначе бот, только гейты
    из TRACKED. Пересечение дат — в пользу коллег (дедуп)."""
    hist_days = set(d.loc[d["src"] == "history", "daykey"])
    keep = ~((d["src"] == "daily") & (d["daykey"].isin(hist_days)))
    d = d[keep].copy()
    ok = [src == "history" or cg in TRACKED.get(ap, set())
          for ap, cg, src in zip(d["airport"], d["cgate"], d["src"])]
    return d[pd.Series(ok, index=d.index)].copy()


def _full_months(d: pd.DataFrame, min_days: int = 26) -> list[str]:
    g = d.groupby("ym")["flight_date"].apply(lambda s: s.dt.date.nunique())
    return sorted([m for m, n in g.items() if n >= min_days])


# ---- Сводка ----------------------------------------------------------------
def build_summary(wb, whole, tracked):
    ws = wb.active
    ws.title = "Сводка"
    ws.cell(1, 1, "Загрузка гейтов — сводка по месяцам, два базиса").font = TITLE_FONT
    ws.cell(2, 1, "«Весь аэропорт» — все рейсы (данные бота). «Наши гейты» — "
                  "только гейты, что ведут коллеги (июнь и ранее — их данные, "
                  "далее — бот). Числа несопоставимы между базисами, сопоставимы "
                  "по месяцам внутри одного базиса.").font = GREY
    r = 4
    maxcols = 1
    for title, data in [("ВЕСЬ АЭРОПОРТ (бот, все гейты)", whole),
                        ("НАШИ ГЕЙТЫ (как у коллег)", tracked)]:
        months = sorted(set(data["ym"]))
        maxcols = max(maxcols, len(months))
        ws.cell(r, 1, title).font = BOLD
        r += 1
        _hdr(ws, r, ["Аэропорт"] + months)
        piv = (data.pivot_table(index="airport", columns="ym", values="cgate",
                                aggfunc="count", fill_value=0)
               .reindex(columns=months, fill_value=0))
        for ap in [a for a in ["SVO", "VKO", "DME"] if a in piv.index]:
            r += 1
            ws.cell(r, 1, ap).font = BOLD
            ws.cell(r, 1).border = BORDER
            for j, m in enumerate(months):
                c = ws.cell(r, 2 + j, int(piv.loc[ap, m]))
                c.font = BASE
                c.border = BORDER
                c.alignment = CENTER
        r += 2
    ws.column_dimensions["A"].width = 12
    for j in range(maxcols):
        ws.column_dimensions[get_column_letter(2 + j)].width = 9
    ws.freeze_panes = "B5"


# ---- Точки -----------------------------------------------------------------
def _series(data, point):
    """Для точки по месяцам: (кол-во рейсов на её гейтах, знаменатель терминала)."""
    ap, term = point["airport"], point["terminal"]
    gates = POINT_GATES[point["point"]]
    sub = data[(data["airport"] == ap) & (data["terminal"] == term)]
    num = sub[sub["cgate"].isin(gates)].groupby("ym").size()
    den = sub.groupby("ym").size()
    return num, den


def build_points(wb, whole, tracked):
    ws = wb.create_sheet("Точки Винегрет", 1)
    ws.cell(1, 1, "Точки Винегрет — доля пасспотока, два базиса").font = TITLE_FONT
    ws.cell(2, 1, "По месяцам: рейсов на гейтах точки; доля в наших гейтах "
                  "терминала (как у коллег); доля во всём терминале (бот). "
                  "Δ — к прошлому полному месяцу по базису «наши гейты».").font = GREY

    months = sorted(set(whole["ym"]) | set(tracked["ym"]))[-6:]
    full = _full_months(tracked)
    last = full[-1] if full else None
    prev = full[-2] if len(full) >= 2 else None

    r0 = 4
    hdr = ["Точка", "Аэр", "Гейты"]
    for m in months:
        hdr += [f"{m}\nрейсов", f"{m}\n%наши", f"{m}\n%всего"]
    hdr += ["Δ наши, пп"]
    _hdr(ws, r0, hdr)
    ws.row_dimensions[r0].height = 26

    row = r0
    for p in POINTS:
        row += 1
        num_t, den_t = _series(tracked, p)
        num_w, den_w = _series(whole, p)
        ws.cell(row, 1, p["point"]).font = BOLD
        ws.cell(row, 2, p["airport"]).font = BASE
        ws.cell(row, 3, ",".join(sorted(POINT_GATES[p["point"]]))).font = Font(FONT, size=8)
        for c in (1, 2, 3):
            ws.cell(row, c).border = BORDER
        col = 4
        share_t = {}
        for m in months:
            n = int(num_t.get(m, 0))
            dt_ = int(den_t.get(m, 0))
            dw = int(den_w.get(m, 0))
            nw = int(num_w.get(m, 0))
            pt = (n / dt_ * 100) if dt_ else 0
            pw = (nw / dw * 100) if dw else 0
            share_t[m] = pt
            for off, val, fmt in [(0, n or None, None),
                                  (1, round(pt, 1) if n else None, '0.0"%"'),
                                  (2, round(pw, 1) if nw else None, '0.0"%"')]:
                c = ws.cell(row, col + off, val)
                c.font = BASE
                c.alignment = CENTER
                c.border = BORDER
                if fmt:
                    c.number_format = fmt
            col += 3
        c = ws.cell(row, col)
        if last and prev:
            dv = round(share_t.get(last, 0) - share_t.get(prev, 0), 1)
            c.value = dv
            c.number_format = "+0.0;-0.0"
            c.font = GREEN if dv >= 3 else RED if dv <= -3 else BASE
        c.border = BORDER
        c.alignment = CENTER

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 6
    ws.column_dimensions["C"].width = 22
    for j in range(len(months) * 3 + 1):
        ws.column_dimensions[get_column_letter(4 + j)].width = 8
    ws.freeze_panes = ws.cell(r0 + 1, 4).coordinate


def _hdr(ws, r, headers):
    for i, h in enumerate(headers, 1):
        c = ws.cell(r, i, h)
        c.fill = HDR_FILL
        c.font = HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER


def make_digest(tracked) -> list[str]:
    full = _full_months(tracked)
    if len(full) < 2:
        return ["Недостаточно полных месяцев для дайджеста."]
    last, prev = full[-1], full[-2]
    lines = [f"Дайджест за {last} (доля в наших гейтах терминала, базис коллег):"]
    ups, downs = [], []
    for p in POINTS:
        num, den = _series(tracked, p)
        cur = (num.get(last, 0) / den.get(last, 1) * 100) if den.get(last, 0) else 0
        pv = (num.get(prev, 0) / den.get(prev, 1) * 100) if den.get(prev, 0) else 0
        dm = cur - pv
        if abs(dm) >= 3:
            (ups if dm > 0 else downs).append(
                f"  {'▲' if dm > 0 else '▼'} {p['point']} ({p['airport']}): "
                f"{cur:.1f}%, {dm:+.1f} пп")
    if ups:
        lines.append("Рост:")
        lines += ups
    if downs:
        lines.append("Снижение:")
        lines += downs
    if not ups and not downs:
        lines.append("  Существенных изменений (≥3 пп) нет.")
    return lines


def build_report(out_path, dfrom=None, dto=None, _df=None):
    if _df is None:
        from src.analytics import load_all
        _df = load_all()
    if _df is None or _df.empty:
        raise SystemExit("Хранилище пусто — нет данных для отчёта.")
    d = _prep(_df, dfrom, dto)
    whole = _whole(d)
    tracked = _tracked(d)
    wb = Workbook()
    build_summary(wb, whole, tracked)
    build_points(wb, whole, tracked)
    wb.save(out_path)
    return {
        "рейсов_всего": int(len(d)),
        "рейсов_бот": int(len(whole)),
        "рейсов_наши_гейты": int(len(tracked)),
        "период": f"{d['ym'].min()}..{d['ym'].max()}",
        "дайджест": make_digest(tracked),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default=None)
    ap.add_argument("--to", dest="dto", default=None)
    ap.add_argument("--out", default="Загрузка_гейтов_отчет.xlsx")
    args = ap.parse_args()
    info = build_report(args.out, args.dfrom, args.dto)
    print(f"Готово: {args.out}")
    for k, v in info.items():
        if k == "дайджест":
            print("\n" + "\n".join(v))
        else:
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
