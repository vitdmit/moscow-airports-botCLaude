"""Аудит дневной выдачи: «никого не потерять».

Считаем диагностические показатели и сохраняем рядом с CSV.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from src.models import FlightDaily, FlightSnapshot, ParsedStatus
from src.utils import get_logger

log = get_logger(__name__)


def build_audit(
    snapshots: list[FlightSnapshot],
    daily: list[FlightDaily],
    target_date: date,
) -> dict:
    """Сводная диагностика по дню.

    Считает: сколько уникальных рейсов в снимках по статусам, сколько
    попало в финал, сколько с review, сколько без гейта.
    Полезно сравнивать день к дню, видно, ломается ли парсинг.
    """
    # Уникальные ключи рейсов в снимках (на день target_date).
    by_airport: dict[str, dict[str, set]] = {
        "SVO": {"all": set(), "departed": set(), "cancelled": set(),
                "delayed_next_day": set()},
        "VKO": {"all": set(), "departed": set(), "cancelled": set(),
                "delayed_next_day": set()},
        "DME": {"all": set(), "departed": set(), "cancelled": set(),
                "delayed_next_day": set()},
    }
    for s in snapshots:
        if s.flight_date != target_date:
            continue
        bucket = by_airport.setdefault(
            s.airport,
            {"all": set(), "departed": set(), "cancelled": set(),
             "delayed_next_day": set()},
        )
        key = (s.scheduled_time, s.destination.strip().lower(),
               frozenset(s.flight_numbers))
        bucket["all"].add(key)
        if s.parsed_status == ParsedStatus.DEPARTED:
            bucket["departed"].add(key)
        elif s.parsed_status == ParsedStatus.CANCELLED:
            bucket["cancelled"].add(key)
        elif s.parsed_status == ParsedStatus.DELAYED_NEXT_DAY:
            bucket["delayed_next_day"].add(key)

    per_airport_summary = {}
    for ap, b in by_airport.items():
        finals = [f for f in daily if f.airport == ap]
        per_airport_summary[ap] = {
            "snapshots_unique_flights": len(b["all"]),
            "snapshots_departed": len(b["departed"]),
            "snapshots_cancelled": len(b["cancelled"]),
            "snapshots_delayed_next_day": len(b["delayed_next_day"]),
            "final_flights": len(finals),
            "final_review_true": sum(1 for f in finals if f.review),
            "final_without_gate": sum(1 for f in finals if not f.gate),
            "final_codeshare_groups": sum(
                1 for f in finals if len(f.flight_numbers) > 1
            ),
        }

    return {
        "target_date": target_date.isoformat(),
        "total_snapshot_rows_considered": sum(
            1 for s in snapshots if s.flight_date == target_date
        ),
        "per_airport": per_airport_summary,
        "notes": [
            "snapshots_departed — нижняя граница «реально вылетевших»: "
            "учитывается только если поллер хотя бы один раз увидел статус "
            "«Вылетел». Если рейс пропал с табло раньше — он попадёт во "
            "final_review_true.",
            "final_without_gate показывает, сколько рейсов даже после всех "
            "снимков остались без гейта (требуется ручной разбор или "
            "увеличение частоты опроса).",
        ],
    }
