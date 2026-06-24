"""Снятие живого табло Яндекс.Расписания (DME, VKO, SVO) ради ГЕЙТОВ.

Зачем: AeroDataBox по части рейсов DME не отдаёт гейт. Яндекс показывает
«Выход на посадку XX» на живом табло, но только пока рейс не улетел. Поэтому
модуль запускается часто (раз в ~10 минут), снимает текущее табло и накапливает
гейты в снапшот-файл. Потом основной отчёт берёт гейт из снапшота.

VKO и SVO снапшотируются для КОНТРОЛЯ (сравнения с ADB), но не для обогащения
основных данных (ADB для них точнее).

Источники:
  DME: https://rasp.yandex.ru/station/9600216/
  VKO: https://rasp.yandex.ru/station/9600215/
  SVO: https://rasp.yandex.ru/station/9600213/

ВАЖНО — учёт перехода через полночь:
  Снапшот, снятый после 20:00 МСК, может содержать рейсы следующего дня
  (ранние вылеты 00:00–06:59 уже видны на ночном табло). Такие рейсы
  записываются в файл D+1.json, а не D.json, чтобы gate enrichment нашёл
  их при обработке нужного дня.

Хранение:
  DME → data/gate_snapshots/YYYY-MM-DD.json   (прежний путь, без изменений)
  VKO → data/gate_snapshots_vko/YYYY-MM-DD.json
  SVO → data/gate_snapshots_svo/YYYY-MM-DD.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import httpx

from src.config import DATA_DIR, AIRPORTS as AIRPORT_CONFIGS
from src.utils import get_logger

log = get_logger("yandex_board")

# Каталоги снапшотов по аэропорту.
# DME — прежний путь, не меняем (в репо уже есть данные).
SNAP_DIRS: dict[str, Path] = {
    "DME": DATA_DIR / "gate_snapshots",
    "VKO": DATA_DIR / "gate_snapshots_vko",
    "SVO": DATA_DIR / "gate_snapshots_svo",
}

MSK = timezone(timedelta(hours=3))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GATE_RE   = re.compile(r"Выход на посадку\s+([A-ZА-Я]?\d+[A-ZА-Я]?)")
FLIGHT_RE = re.compile(r"\b([A-Z0-9]{2})\s?(\d{1,4})\b")
TIME_RE   = re.compile(r"\b([0-2]\d:[0-5]\d)\b")


def _board_url(airport: str) -> str:
    """URL живого табло вылетов для аэропорта."""
    sid = AIRPORT_CONFIGS[airport]["station_id"]
    return f"https://rasp.yandex.ru/station/{sid}/"


def _terminal_of(gate: str) -> str:
    """D4→D, C14→C, E8→E. Если только цифры — терминал неизвестен."""
    m = re.match(r"^([A-ZА-Я])", gate)
    return m.group(1) if m else ""


def fetch_board_html(airport: str = "DME",
                     client: httpx.Client | None = None) -> str:
    url = _board_url(airport)
    own = client is None
    if own:
        client = httpx.Client(timeout=30, headers={"User-Agent": UA},
                              follow_redirects=True)
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.text or ""
    finally:
        if own:
            client.close()


def parse_board(html: str) -> list[dict]:
    """Строки рейсов с объявленными гейтами. Возвращает {flight, time, gate, terminal}."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S):
        text = re.sub(r"<[^>]+>", " ", tr)
        text = re.sub(r"\s+", " ", text).strip()
        gm = GATE_RE.search(text)
        if not gm:
            continue
        gate = gm.group(1).strip()
        tmt = TIME_RE.search(text)
        # Номер рейса: сначала из href, потом из текста
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


def _flight_day(flight_time: str, capture_dt: datetime) -> date:
    """Определить дату рейса при захвате снапшота.

    Правило: если сейчас позднее 20:00 МСК и время рейса попадает в диапазон
    00:00–06:59 — это рейс СЛЕДУЮЩЕГО дня (уже виден на ночном табло заранее).
    Все прочие сочетания → текущий день.

    Это решает проблему «перехода через полночь»: задержанные рейсы, чей гейт
    объявили после 00:00, и ранние рейсы D+1 попадают в правильный файл.
    """
    if not flight_time:
        return capture_dt.date()
    try:
        hour = int(flight_time.split(":")[0])
        if capture_dt.hour >= 20 and hour < 7:
            return (capture_dt + timedelta(days=1)).date()
    except (ValueError, IndexError):
        pass
    return capture_dt.date()


def _load_store(snap_dir: Path, d: date) -> dict:
    path = snap_dir / f"{d.isoformat()}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def snapshot(airport: str = "DME",
             day: date | None = None,
             client: httpx.Client | None = None) -> dict:
    """Снять текущее табло и слить в снапшот дня.

    Учитывает переход через полночь: рейсы 00:00–06:59, видимые после 20:00,
    сохраняются в D+1.json, а не в D.json — это их реальный день вылета.
    Идемпотентно: повторные снимки дополняют и уточняют (при смене гейта
    записывается актуальный).

    Возвращает снапшот текущего дня (словарь из снапшота D).
    """
    now = datetime.now(tz=MSK)
    d = day or now.date()
    snap_dir = SNAP_DIRS.get(airport,
                              DATA_DIR / f"gate_snapshots_{airport.lower()}")
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Предзагружаем снапшот текущего дня
    stores: dict[date, dict] = {d: _load_store(snap_dir, d)}

    html = fetch_board_html(airport, client)
    rows = parse_board(html)

    added_today = 0
    tomorrow_cnt = 0

    for r in rows:
        r_date = _flight_day(r["time"], now)
        if r_date not in stores:
            stores[r_date] = _load_store(snap_dir, r_date)
        if r_date > d:
            tomorrow_cnt += 1

        tgt = stores[r_date]
        key = f"{r['flight']}|{r['time']}"
        entry = {"flight": r["flight"], "time": r["time"],
                 "gate": r["gate"], "terminal": r["terminal"]}
        if key not in tgt or not tgt[key].get("gate"):
            tgt[key] = entry
            if r_date == d:
                added_today += 1
        elif tgt[key].get("gate") != r["gate"]:
            # Гейт сменился (нередко) — берём последний актуальный
            tgt[key].update({"gate": r["gate"], "terminal": r["terminal"]})

    # Сохраняем все затронутые снапшоты
    for snap_date, snap_data in stores.items():
        p = snap_dir / f"{snap_date.isoformat()}.json"
        p.write_text(json.dumps(snap_data, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    log.info(
        "[%s] Снимок %s %s: строк с гейтом %d (%d→D+1). "
        "Снапшот [%s]: %d записей (+%d новых)",
        airport, d, now.strftime("%H:%M"), len(rows), tomorrow_cnt,
        d, len(stores[d]), added_today,
    )
    return stores[d]


def load_snapshot(d: date, airport: str = "DME") -> dict:
    """Загрузить снапшот гейтов за день. Пустой dict если нет данных."""
    snap_dir = SNAP_DIRS.get(airport,
                              DATA_DIR / f"gate_snapshots_{airport.lower()}")
    return _load_store(snap_dir, d)


if __name__ == "__main__":
    airport_arg = sys.argv[1].upper() if len(sys.argv) > 1 else "DME"
    if airport_arg not in SNAP_DIRS:
        print(f"Неизвестный аэропорт: {airport_arg}. Доступны: {', '.join(SNAP_DIRS)}")
        sys.exit(1)
    snapshot(airport_arg)
