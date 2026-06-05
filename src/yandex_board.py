"""Снятие живого табло Яндекс.Расписания (DME) ради НОМЕРОВ ГЕЙТОВ.

Зачем: AeroDataBox по части рейсов DME не отдаёт гейт. Яндекс показывает
«Выход на посадку XX» на живом табло, но только пока рейс не улетел. Поэтому
этот модуль запускается часто (раз в ~10 минут), снимает текущее табло и
накапливает гейты в снапшот-файл. Потом основной отчёт берёт гейт из снапшота,
если у AeroDataBox его нет.

Источник: https://rasp.yandex.ru/station/9600216/ (DME, вылет).
Достаём по строке: время, номер рейса, направление, гейт, терминал.

ВАЖНО: это вспомогательный источник ТОЛЬКО для гейтов DME. Состав рейсов и
факт вылета берём из AeroDataBox (основной источник истины).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import httpx

from src.config import DATA_DIR
from src.utils import get_logger

log = get_logger("yandex_board")

DME_URL = "https://rasp.yandex.ru/station/9600216/"
SNAP_DIR = DATA_DIR / "gate_snapshots"
MSK = timezone(timedelta(hours=3))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GATE_RE = re.compile(r"Выход на посадку\s+([A-ZА-Я]?\d+[A-ZА-Я]?)")
# номер рейса вида "U6 1343", "S7 4263", "3F 314"
FLIGHT_RE = re.compile(r"\b([A-Z0-9]{2})\s?(\d{1,4})\b")
TIME_RE = re.compile(r"\b([0-2]\d:[0-5]\d)\b")


def _terminal_of(gate: str) -> str:
    """D4->D, C14->C, E8->E. Если только цифры — терминал неизвестен."""
    m = re.match(r"^([A-ZА-Я])", gate)
    return m.group(1) if m else ""


def fetch_board_html(client: httpx.Client | None = None) -> str:
    own = client is None
    if own:
        client = httpx.Client(timeout=30, headers={"User-Agent": UA},
                              follow_redirects=True)
    try:
        r = client.get(DME_URL)
        r.raise_for_status()
        return r.text or ""
    finally:
        if own:
            client.close()


def parse_board(html: str) -> list[dict]:
    """Достать из HTML строки рейсов с гейтами. Возвращает список:
    {flight, time, gate, terminal}. Берём только строки, где есть гейт."""
    rows = []
    # таблица: каждая запись рейса — это блок строки <tr>...</tr>
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S):
        text = re.sub(r"<[^>]+>", " ", tr)
        text = re.sub(r"\s+", " ", text).strip()
        gm = GATE_RE.search(text)
        if not gm:
            continue
        gate = gm.group(1).strip()
        # время — первое в строке
        tmt = TIME_RE.search(text)
        # номер рейса: ищем в href travel.yandex или в тексте
        fm = re.search(r"flights/([A-Z0-9]{2})-(\d{1,4})/", tr)
        if fm:
            flight = f"{fm.group(1)} {fm.group(2)}"
        else:
            fm2 = FLIGHT_RE.search(text)
            flight = f"{fm2.group(1)} {fm2.group(2)}" if fm2 else ""
        if not flight:
            continue
        rows.append({
            "flight": flight,
            "time": tmt.group(1) if tmt else "",
            "gate": gate,
            "terminal": _terminal_of(gate),
        })
    return rows


def snapshot(day: date | None = None, client: httpx.Client | None = None) -> dict:
    """Снять текущее табло и слить в снапшот дня. Снапшот — это «лучшее знание»
    о гейтах за день: ключ = (рейс, время), значение = гейт/терминал. Повторные
    снимки дополняют и обновляют. Идемпотентно."""
    now = datetime.now(tz=MSK)
    d = day or now.date()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"{d.isoformat()}.json"
    store = {}
    if path.exists():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            store = {}

    html = fetch_board_html(client)
    rows = parse_board(html)
    added = 0
    for r in rows:
        key = f"{r['flight']}|{r['time']}"
        if key not in store or not store[key].get("gate"):
            store[key] = {"flight": r["flight"], "time": r["time"],
                          "gate": r["gate"], "terminal": r["terminal"]}
            added += 1
        elif store[key].get("gate") != r["gate"]:
            # гейт сменился (бывает) — оставляем последний известный
            store[key].update({"gate": r["gate"], "terminal": r["terminal"]})

    path.write_text(json.dumps(store, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    log.info("Снимок табло DME %s: строк с гейтом %d, всего в снапшоте %d (+%d)",
             now.strftime("%H:%M"), len(rows), len(store), added)
    return store


def load_snapshot(d: date) -> dict:
    path = SNAP_DIR / f"{d.isoformat()}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    snapshot()
