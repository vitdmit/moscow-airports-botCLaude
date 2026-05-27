"""Модели данных проекта.

FlightSnapshot — один наблюдаемый рейс в одном тике поллера.
FlightDaily   — финальная запись о фактически вылетевшем рейсе за сутки.
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ParsedStatus(str, Enum):
    """Нормализованный статус рейса."""

    SCHEDULED = "scheduled"           # «Вылет по расписанию»
    BOARDING = "boarding"             # «Идёт посадка», «Выход на посадку XX»
    REGISTRATION = "registration"     # «Стойки регистрации XX»
    DEPARTED = "departed"             # «Вылетел»
    CANCELLED = "cancelled"           # «Отменён»
    DELAYED_SAME_DAY = "delayed_same_day"   # задержан в пределах суток
    DELAYED_NEXT_DAY = "delayed_next_day"   # задержан на следующие сутки
    UNKNOWN = "unknown"


class FlightSnapshot(BaseModel):
    """Снимок одной строки табло в момент опроса.

    Все строки одного тика складываются в один JSONL-файл
    data/snapshots/YYYY-MM-DD/HHMM_<AIRPORT>.jsonl
    """

    airport: str = Field(description="IATA-код: SVO / VKO / DME")
    snapshot_at: datetime = Field(description="UTC время снимка")
    flight_date: date = Field(description="Дата рейса по расписанию, MSK")
    scheduled_time: time
    delayed_until: Optional[datetime] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    airlines: list[str] = Field(default_factory=list)
    flight_numbers: list[str] = Field(default_factory=list)
    destination: str
    raw_status: str
    parsed_status: ParsedStatus


class FlightDaily(BaseModel):
    """Финальная запись о фактически вылетевшем рейсе за сутки."""

    airport: str
    flight_date: date
    scheduled_time: time
    actual_time: Optional[time] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    airlines: list[str] = Field(default_factory=list)
    flight_numbers: list[str] = Field(default_factory=list)
    destination: str
    snapshots_seen: int = Field(
        description="Сколько раз рейс наблюдался поллером (для аудита)"
    )
    review: bool = False
    review_reason: Optional[str] = None
