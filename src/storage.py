"""Запись и чтение данных (JSONL-снапшоты + дневные CSV)."""
from __future__ import annotations

import csv
import gzip
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

from src.config import DAILY_DIR, MSK, SNAPSHOTS_DIR
from src.models import FlightDaily, FlightSnapshot


# Параллельно с распарсенными JSONL храним сырой HTML (gzip) —
# чтобы при любом будущем баге парсера можно было перепарсить задним числом.
RAW_DIR = SNAPSHOTS_DIR.parent / "raw_html"


# ---------- снапшоты (raw) ----------

def snapshot_path(airport: str, ts: datetime) -> Path:
    """Путь к JSONL-файлу одного тика поллера.

    Пример: data/snapshots/2026-05-27/1340_SVO.jsonl
    Время в имени — UTC, отдельная папка под дату MSK для удобства аналитики.
    """
    msk_dt = ts.astimezone(MSK)
    day_dir = SNAPSHOTS_DIR / msk_dt.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{msk_dt.strftime('%H%M')}_{airport}.jsonl"
    return day_dir / fname


def write_snapshot(airport: str, ts: datetime, rows: Iterable[FlightSnapshot]) -> Path:
    """Записать снапшот рейсов в JSONL.

    Каждая строка файла — один сериализованный FlightSnapshot.
    """
    path = snapshot_path(airport, ts)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(row.model_dump_json() + "\n")
    return path


def write_raw_html(airport: str, ts: datetime, html_text: str) -> Path:
    """Сохранить сырой HTML страницы под gzip.

    Пример: data/raw_html/2026-05-27/1340_SVO.html.gz
    Нужно для возможности перепарсить старые снапшоты при фиксе парсера.
    Размер ~10 КБ после gzip; для 3 аэропортов × 144 тиков = ~4 МБ в день.
    """
    msk_dt = ts.astimezone(MSK)
    day_dir = RAW_DIR / msk_dt.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{msk_dt.strftime('%H%M')}_{airport}.html.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
        f.write(html_text)
    return path


def iter_snapshots_for_day(day: date) -> Iterable[FlightSnapshot]:
    """Прочитать все снапшоты за указанные сутки (MSK).

    Возвращает плоский поток FlightSnapshot из всех файлов.
    """
    day_dir = SNAPSHOTS_DIR / day.isoformat()
    if not day_dir.exists():
        return
    for path in sorted(day_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield FlightSnapshot.model_validate(json.loads(line))


# ---------- дневная CSV ----------

DAILY_FIELDS = [
    "airport", "flight_date", "scheduled_time", "actual_time",
    "terminal", "gate", "airlines", "flight_numbers", "destination",
    "snapshots_seen", "review", "review_reason",
]


def write_daily_csv(day: date, rows: list[FlightDaily]) -> Path:
    """Записать финальный CSV за сутки.

    Массивы (airlines, flight_numbers) сериализуем через ';' —
    это удобнее парсить в Excel, чем JSON.
    """
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day.isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "airport": r.airport,
                "flight_date": r.flight_date.isoformat(),
                "scheduled_time": r.scheduled_time.isoformat(timespec="minutes"),
                "actual_time": r.actual_time.isoformat(timespec="minutes")
                    if r.actual_time else "",
                "terminal": r.terminal or "",
                "gate": r.gate or "",
                "airlines": ";".join(r.airlines),
                "flight_numbers": ";".join(r.flight_numbers),
                "destination": r.destination,
                "snapshots_seen": r.snapshots_seen,
                "review": "1" if r.review else "0",
                "review_reason": r.review_reason or "",
            })
    return path


def write_audit(day: date, audit: dict) -> Path:
    """Записать .audit.json с результатами проверок целостности."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day.isoformat()}.audit.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=2, default=str)
    return path
