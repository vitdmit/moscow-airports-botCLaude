"""Карта точек (ресторанов/складов) Винегрет и привязанных к ним гейтов.

Источник — файл «Точки_и_гейты.xlsx». Каждая точка стоит в зоне вылета рядом
с гейтами; пассажиры этих гейтов формируют пасспоток мимо точки -> влияют на
товарооборот. Поэтому в аналитике эти гейты — фокус.

Правило диапазона: «11-12» = все реально существующие гейты по порядку между
границами включительно, вместе с буквенными вариантами (24-25 -> 24,24А,25,25А).

Аэропорт у каждой точки задан ЯВНО: номера гейтов пересекаются между
аэропортами (13/16/17 есть и в SVO-D, и в VKO-A), поэтому привязка по номеру
была бы неоднозначной.
"""
from __future__ import annotations
import re

# Точка -> (аэропорт, терминал, спецификация гейтов).
# Спецификация: список диапазонов/одиночных номеров; буквенные варианты (А)
# подхватываются автоматически из фактических данных.
POINTS = [
    # VKO, терминал A
    {"point": "АБ460",       "airport": "VKO", "terminal": "A", "gates": "11-12"},
    {"point": "Кофеин ВВЛ",  "airport": "VKO", "terminal": "A", "gates": "13"},
    {"point": "Бад",         "airport": "VKO", "terminal": "A", "gates": "15-16"},
    {"point": "Бир3",        "airport": "VKO", "terminal": "A", "gates": "24-25"},
    {"point": "Кофеин МВЛ",  "airport": "VKO", "terminal": "A", "gates": "24-25"},
    {"point": "Ачарули",     "airport": "VKO", "terminal": "A", "gates": "23-24"},
    {"point": "АБ99",        "airport": "VKO", "terminal": "A", "gates": "21-22"},
    # DME
    {"point": "Баттерфляй",  "airport": "DME", "terminal": "C", "gates": "C5-C7"},
    {"point": "АБ131",       "airport": "DME", "terminal": "D", "gates": "D3-D8"},
    {"point": "Гурмэ Т2",    "airport": "DME", "terminal": "E", "gates": "E13,E14"},
    # SVO
    {"point": "АБ600",       "airport": "SVO", "terminal": "D", "gates": "D16-D17"},
    {"point": "Гурмэ В",     "airport": "SVO", "terminal": "B", "gates": "117-121"},
]


def _split_gate(g: str):
    """'D17' -> ('D', 17); '24' -> ('', 24); '25A' -> ('', 25) с буквой отдельно."""
    g = str(g).strip().upper()
    m = re.match(r"^([A-ZА-Я]*)(\d+)([A-ZА-Я]?)$", g)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def expand_gates(spec: str, available: set[str]) -> list[str]:
    """Развернуть спецификацию ('11-12', 'C5-C7', 'E13,E14') в список реальных
    гейтов из множества `available` (фактически встречающихся в данных).
    Диапазон включает все номера между границами и их буквенные варианты."""
    result: list[str] = []
    for token in str(spec).split(","):
        token = token.strip().upper()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            pa, pb = _split_gate(a), _split_gate(b)
            if not pa or not pb:
                continue
            prefix = pa[0] or pb[0]
            lo, hi = sorted([pa[1], pb[1]])
            for num in range(lo, hi + 1):
                # все реальные гейты с этим префиксом+номером (с буквой и без)
                for g in available:
                    pg = _split_gate(g)
                    if pg and pg[0] == prefix and pg[1] == num:
                        result.append(g)
        else:
            pt = _split_gate(token)
            if not pt:
                continue
            for g in available:
                pg = _split_gate(g)
                if pg and pg[0] == pt[0] and pg[1] == pt[1]:
                    # если в споке указана конкретная буква — точное совпадение
                    if pt[2] and pg[2] != pt[2]:
                        continue
                    result.append(g)
    # уникальные, в порядке номера+буквы
    seen, out = set(), []
    for g in sorted(result, key=lambda x: (_split_gate(x)[1], _split_gate(x)[2])):
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def points_for(airport: str):
    return [p for p in POINTS if p["airport"] == airport]
