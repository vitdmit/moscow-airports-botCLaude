"""Генерация Excel-отчёта загрузки гейтов: помесячно + год-к-году.

Вкладки:
  Сводка        — все аэропорты, рейсов по месяцам (обзор сверху).
  SVO / VKO / DME — по каждому: терминал -> зона (МВЛ/ВВЛ) -> гейт,
                    доля гейта внутри зоны по месяцам + Δ год-к-году.

Метрика в ячейках — доля гейта внутри его (терминал, зона) за месяц, %.
Сумма долей гейтов одной (терминал, зона) за месяц = 100%.

Запуск:
    python -m src.report                      # весь период
    python -m src.report --from 2025-01 --to 2026-05
    python -m src.report --out report.xlsx

Источник — единое хранилище (история + ежедневные сборы) через analytics.load_all().
"""
from __future__ import annotations

import argparse
from datetime import date

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from src.analytics import load_all
from src.zones import zone
from src.points import POINTS, expand_gates

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F4E78")
HDR_FONT = Font(FONT, bold=True, color="FFFFFF", size=10)
SUB_FILL = PatternFill("solid", fgColor="D9E1F2")
ZONE_FILL = PatternFill("solid", fgColor="E2EFDA")
TOTAL_FILL = PatternFill("solid", fgColor="FCE4D6")
TITLE_FONT = Font(FONT, bold=True, size=14, color="1F4E78")
BASE = Font(FONT, size=10)
BOLD = Font(FONT, bold=True, size=10)
GREY = Font(FONT, size=9, color="808080")
THIN = Side(style="thin", color="D0D0D0")
BORDER = Border(THIN, THIN, THIN, THIN)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")


def _prep(df: pd.DataFrame, dfrom: str | None, dto: str | None) -> pd.DataFrame:
    d = df.copy()
    d["ym"] = d["flight_date"].dt.strftime("%Y-%m")
    if dfrom:
        d = d[d["ym"] >= dfrom]
    if dto:
        d = d[d["ym"] <= dto]
    d["zone"] = d["destination"].apply(zone)
    d["gate"] = d["gate"].fillna("—").astype(str).str.strip().replace("", "—")
    d["terminal"] = d["terminal"].fillna("—").astype(str).str.strip().replace("", "—")
    return d


def _style_header(ws, row, headers, start_col=1):
    for i, h in enumerate(headers):
        c = ws.cell(row, start_col + i, h)
        c.fill = HDR_FILL
        c.font = HDR_FONT
        c.border = BORDER
        c.alignment = CENTER


def build_summary(wb, d):
    ws = wb.active
    ws.title = "Сводка"
    ws.cell(1, 1, "Загрузка гейтов московских аэропортов — сводка по месяцам").font = TITLE_FONT
    ws.cell(2, 1, "Рейсов (фактически вылетевших) по аэропортам и месяцам. "
                  "Деление на зоны и гейты — на отдельных вкладках.").font = GREY

    months = sorted(d["ym"].unique())
    piv = d.pivot_table(index="airport", columns="ym", values="gate",
                        aggfunc="count", fill_value=0)
    piv = piv.reindex(columns=months, fill_value=0)

    r0 = 4
    _style_header(ws, r0, ["Аэропорт"] + months)
    airports = [a for a in ["SVO", "VKO", "DME"] if a in piv.index]
    for i, ap in enumerate(airports):
        r = r0 + 1 + i
        c = ws.cell(r, 1, ap)
        c.font = BOLD
        c.border = BORDER
        for j, m in enumerate(months):
            cell = ws.cell(r, 2 + j, int(piv.loc[ap, m]))
            cell.font = BASE
            cell.border = BORDER
            cell.alignment = CENTER
    # строка ИТОГО формулой
    rt = r0 + 1 + len(airports)
    ws.cell(rt, 1, "ИТОГО").font = BOLD
    ws.cell(rt, 1).fill = TOTAL_FILL
    ws.cell(rt, 1).border = BORDER
    for j in range(len(months)):
        col = get_column_letter(2 + j)
        cell = ws.cell(rt, 2 + j, f"=SUM({col}{r0+1}:{col}{rt-1})")
        cell.font = BOLD
        cell.fill = TOTAL_FILL
        cell.border = BORDER
        cell.alignment = CENTER

    ws.column_dimensions["A"].width = 12
    for j in range(len(months)):
        ws.column_dimensions[get_column_letter(2 + j)].width = 9
    ws.freeze_panes = "B5"


def build_airport_sheet(wb, d, airport, n_weeks=8):
    """Детальный лист аэропорта в формате коллег: гейты построчно,
    сгруппированы по терминалу и зоне (ВВЛ/МВЛ). По колонкам — последние
    n_weeks недель + все месяцы, в каждом периоде пара «кол-во | %».
    % = доля гейта внутри (терминал, зона) за период. Снизу строка ИТОГО."""
    sub = d[d["airport"] == airport].copy()
    if sub.empty:
        return
    ws = wb.create_sheet(airport)
    sub["week"] = sub["flight_date"].dt.strftime("%G-W%V")
    weeks = sorted(sub["week"].unique())[-n_weeks:]
    months = sorted(sub["ym"].unique())

    ws.cell(1, 1, f"{airport} — детальная загрузка гейтов по зонам").font = TITLE_FONT
    ws.cell(2, 1, f"Кол-во рейсов и доля гейта (%) внутри своей зоны терминала. "
                  f"Периоды: последние {len(weeks)} недель и все месяцы. "
                  f"Сумма % гейтов одной зоны за период = 100%.").font = GREY

    # периоды: (метка, тип, значение-колонка в данных)
    periods = [("W:" + w, "week", w) for w in weeks] + \
              [("M:" + m, "ym", m) for m in months]

    # шапка: 3 строки. R4 — группа период (merged на 2 колонки), R5 — кол-во/%
    r_grp, r_sub, r_data = 4, 5, 6
    ws.cell(r_grp, 1, "Терминал").font = HDR_FONT
    ws.cell(r_grp, 1).fill = HDR_FILL
    ws.cell(r_grp, 2, "Зона").font = HDR_FONT
    ws.cell(r_grp, 2).fill = HDR_FILL
    ws.cell(r_grp, 3, "Гейт").font = HDR_FONT
    ws.cell(r_grp, 3).fill = HDR_FILL
    for c in (1, 2, 3):
        ws.cell(r_grp, c).alignment = CENTER
        ws.cell(r_grp, c).border = BORDER
        ws.merge_cells(start_row=r_grp, start_column=c, end_row=r_sub, end_column=c)
    col = 4
    for label, _, _ in periods:
        nice = label[2:].replace("W", "нед.") if label.startswith("W:") else label[2:]
        ws.merge_cells(start_row=r_grp, start_column=col, end_row=r_grp, end_column=col + 1)
        gc = ws.cell(r_grp, col, nice)
        gc.fill = HDR_FILL; gc.font = HDR_FONT; gc.alignment = CENTER; gc.border = BORDER
        for off, t in enumerate(["кол", "%"]):
            sc = ws.cell(r_sub, col + off, t)
            sc.fill = SUB_FILL; sc.font = Font(FONT, bold=True, size=8)
            sc.alignment = CENTER; sc.border = BORDER
        col += 2
    last_col = col - 1

    # данные: число рейсов (терминал,зона,гейт,период) и знаменатель (терминал,зона,период)
    def counts(period_type):
        n = sub.groupby(["terminal", "zone", "gate", period_type]).size()
        den = sub.groupby(["terminal", "zone", period_type]).size()
        return n, den
    wn, wden = counts("week")
    mn, mden = counts("ym")

    row = r_data
    zone_order = {"ВВЛ": 0, "МВЛ": 1, "?": 2}
    for term in sorted(sub["terminal"].unique()):
        for z in sorted(sub[sub.terminal == term]["zone"].unique(),
                        key=lambda x: zone_order.get(x, 9)):
            zlabel = {"ВВЛ": "Внутренние (ВВЛ)", "МВЛ": "Международные (МВЛ)",
                      "?": "Не классиф."}.get(z, z)
            # заголовок зоны
            ws.cell(row, 1, f"Терм. {term}").font = BOLD
            ws.cell(row, 2, zlabel).font = BOLD
            for c in range(1, last_col + 1):
                ws.cell(row, c).fill = ZONE_FILL
            row += 1
            zgates = sorted(sub[(sub.terminal == term) & (sub.zone == z)]["gate"].unique())
            block_start = row
            for gate in zgates:
                ws.cell(row, 3, gate).font = BASE
                ws.cell(row, 3).alignment = CENTER; ws.cell(row, 3).border = BORDER
                c = 4
                for label, ptype, pval in periods:
                    if ptype == "week":
                        cnt = int(wn.get((term, z, gate, pval), 0))
                        den = int(wden.get((term, z, pval), 0))
                    else:
                        cnt = int(mn.get((term, z, gate, pval), 0))
                        den = int(mden.get((term, z, pval), 0))
                    pct = (cnt / den * 100) if den else 0
                    cc = ws.cell(row, c, cnt if cnt else None)
                    cc.font = BASE; cc.alignment = CENTER; cc.border = BORDER
                    pc = ws.cell(row, c + 1, round(pct, 0) / 100 if cnt else None)
                    pc.number_format = "0%"; pc.font = BASE
                    pc.alignment = CENTER; pc.border = BORDER
                    c += 2
                row += 1
            # ИТОГО зоны
            ws.cell(row, 2, "ИТОГО зоны").font = BOLD
            for c in range(1, last_col + 1):
                ws.cell(row, c).fill = TOTAL_FILL
            cc = 4
            for label, ptype, pval in periods:
                den = int((wden if ptype == "week" else mden).get((term, z, pval), 0))
                tc = ws.cell(row, cc, den if den else None)
                tc.font = BOLD; tc.alignment = CENTER; tc.border = BORDER
                ws.cell(row, cc + 1, None).border = BORDER
                cc += 2
            row += 2

    ws.column_dimensions["A"].width = 11
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 7
    for c in range(4, last_col + 1):
        ws.column_dimensions[get_column_letter(c)].width = 5.5
    ws.freeze_panes = ws.cell(r_data, 4).coordinate


def _point_share_series(d, point, available):
    """Для точки: доля её гейтов внутри (терминал) по месяцам.
    Берём долю в ТЕРМИНАЛЕ (а не зоне) — пасспоток мимо точки идёт по всему
    терминалу независимо от направления рейса."""
    gates = expand_gates(point["gates"], available)
    if not gates:
        return None, []
    sub = d[(d.airport == point["airport"]) & (d.terminal == point["terminal"])]
    months = sorted(sub["ym"].unique())
    # числитель: рейсы с гейтов точки; знаменатель: рейсы терминала
    num = (sub[sub.gate.isin(gates)].groupby("ym").size())
    den = sub.groupby("ym").size()
    share = (num / den * 100).reindex(months).fillna(0)
    cnt = num.reindex(months).fillna(0).astype(int)
    return pd.DataFrame({"ym": months,
                         "share": share.values,
                         "n": cnt.values}), gates


def build_points_sheet(wb, d):
    """Фокус-вкладка: только точки Винегрет. Доля пасспотока (гейты точки в
    терминале) по месяцам + Δ к прошлому месяцу и к тому же месяцу год назад."""
    ws = wb.create_sheet("Точки Винегрет", 1)  # сразу после Сводки
    ws.cell(1, 1, "Точки Винегрет — доля пасспотока по гейтам рядом с точкой").font = TITLE_FONT
    ws.cell(2, 1, "Доля = рейсов с гейтов точки ÷ рейсов терминала за месяц. "
                  "Δ мес — к прошлому месяцу; Δ г/г — к тому же месяцу год назад "
                  "(процентные пункты). Это доля пасспотока мимо точки.").font = GREY

    months_all = sorted(d["ym"].unique())
    full = _full_months(d)
    last = full[-1] if full else None
    prev = full[-2] if len(full) >= 2 else None
    yoy = None
    if last:
        cand = f"{int(last[:4])-1}-{last[5:]}"
        yoy = cand if cand in full else None

    # последние до 6 месяцев для компактности (по всем, но неполный пометим)
    show_months = months_all[-6:]
    incomplete = set(months_all) - set(full)
    r0 = 4
    headers = (["Точка", "Аэропорт", "Гейты"]
               + [m + ("*" if m in incomplete else "") for m in show_months]
               + ["Δ мес, пп", "Δ г/г, пп"])
    _style_header(ws, r0, headers)

    avail = {ap: set(d[d.airport == ap].gate.dropna().astype(str))
             for ap in ["VKO", "DME", "SVO"]}
    row = r0 + 1
    for p in POINTS:
        series, gates = _point_share_series(d, p, avail[p["airport"]])
        if series is None:
            continue
        smap = series.set_index("ym")["share"]
        ws.cell(row, 1, p["point"]).font = BOLD
        ws.cell(row, 1).border = BORDER
        ws.cell(row, 2, p["airport"]).font = BASE
        ws.cell(row, 2).border = BORDER; ws.cell(row, 2).alignment = CENTER
        gc = ws.cell(row, 3, ",".join(gates))
        gc.font = Font(FONT, size=8); gc.border = BORDER
        for j, m in enumerate(show_months):
            v = smap.get(m, 0)
            cell = ws.cell(row, 4 + j, round(v, 1) if v else None)
            cell.number_format = '0.0"%"'; cell.font = BASE
            cell.border = BORDER; cell.alignment = CENTER
        # Δ месяц
        cm = 4 + len(show_months)
        dmes = (smap.get(last, 0) - smap.get(prev, 0)) if (last and prev) else None
        dyoy = (smap.get(last, 0) - smap.get(yoy, 0)) if (last and yoy) else None
        for off, val in [(0, dmes), (1, dyoy)]:
            cell = ws.cell(row, cm + off)
            if val is not None:
                cell.value = round(val, 1)
                cell.number_format = '+0.0;-0.0'
                # подсветка значимых изменений (|Δ| >= 3 пп)
                if val >= 3:
                    cell.fill = PatternFill("solid", fgColor="C6EFCE")
                    cell.font = Font(FONT, size=10, color="006100", bold=True)
                elif val <= -3:
                    cell.fill = PatternFill("solid", fgColor="FFC7CE")
                    cell.font = Font(FONT, size=10, color="9C0006", bold=True)
                else:
                    cell.font = BASE
            cell.border = BORDER; cell.alignment = CENTER
        row += 1

    if incomplete:
        ws.cell(row + 1, 1, "* месяц неполный (данные не за все дни) — "
                "в расчёт Δ не берётся.").font = GREY

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 26
    for j in range(len(show_months)):
        ws.column_dimensions[get_column_letter(4 + j)].width = 9
    ws.column_dimensions[get_column_letter(4 + len(show_months))].width = 11
    ws.column_dimensions[get_column_letter(5 + len(show_months))].width = 11
    ws.freeze_panes = ws.cell(r0 + 1, 4).coordinate


def _full_months(d, min_days: int = 26) -> list[str]:
    """Месяцы, где есть данные минимум за min_days дней — считаем полными.
    Неполный текущий месяц (напр. 2 дня) исключаем, чтобы не искажать Δ."""
    days = d.groupby("ym")["flight_date"].apply(lambda s: s.dt.date.nunique())
    return sorted([m for m, n in days.items() if n >= min_days])


def make_digest(d) -> list[str]:
    """Краткий текстовый дайджест важных изменений по точкам за последний
    ПОЛНЫЙ месяц. Антипод 'портянки': только значимое (|Δ| >= 3 пп)."""
    months = _full_months(d)
    if len(months) < 2:
        return ["Недостаточно полных месяцев для дайджеста."]
    last, prev = months[-1], months[-2]
    yoy = f"{int(last[:4])-1}-{last[5:]}"
    yoy = yoy if yoy in months else None
    avail = {ap: set(d[d.airport == ap].gate.dropna().astype(str))
             for ap in ["VKO", "DME", "SVO"]}
    lines = [f"Дайджест за {last} (изменения доли пасспотока у точек):"]
    ups, downs = [], []
    for p in POINTS:
        series, _ = _point_share_series(d, p, avail[p["airport"]])
        if series is None:
            continue
        smap = series.set_index("ym")["share"]
        cur = smap.get(last, 0)
        dm = cur - smap.get(prev, 0)
        line = f"{p['point']} ({p['airport']}): {cur:.1f}%"
        if abs(dm) >= 3:
            arrow = "▲" if dm > 0 else "▼"
            (ups if dm > 0 else downs).append(f"  {arrow} {line}, {dm:+.1f} пп к прошлому месяцу")
    if ups:
        lines.append("Рост:")
        lines += ups
    if downs:
        lines.append("Снижение:")
        lines += downs
    if not ups and not downs:
        lines.append("  Существенных изменений (≥3 пп) нет — загрузка стабильна.")
    return lines


def build_report(out_path, dfrom=None, dto=None):
    df = load_all()
    if df.empty:
        raise SystemExit("Хранилище пусто — нет данных для отчёта.")
    d = _prep(df, dfrom, dto)
    wb = Workbook()
    build_summary(wb, d)
    build_points_sheet(wb, d)
    for ap in ["SVO", "VKO", "DME"]:
        build_airport_sheet(wb, d, ap)
    wb.save(out_path)
    digest = make_digest(d)
    info = {
        "рейсов": len(d),
        "период": f"{d['ym'].min()}..{d['ym'].max()}",
        "не классифицировано (зона ?)": int((d["zone"] == "?").sum()),
        "дайджест": digest,
    }
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default=None, help="с месяца YYYY-MM")
    ap.add_argument("--to", dest="dto", default=None, help="по месяц YYYY-MM")
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
