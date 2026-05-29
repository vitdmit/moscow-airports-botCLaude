"""Группировка снимков в финальные записи рейсов + склейка кодшерингов.

Ключевые правила (согласованы с заказчиком):
 1) Один физический борт = одна запись.
 2) Кодшеринг определяется как (одинаковое направление + одинаковый
    гейт + время вылета совпадает в пределах ±2 минут). В этом случае
    flight_numbers и airlines склеиваются.
 3) Учитываются только рейсы со статусом «вылетел». Отменённые и
    задержанные на следующие сутки — игнорируются.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable

from src.models import FlightDaily, FlightSnapshot, ParsedStatus
from src.utils import get_logger

log = get_logger(__name__)


# ---------- группировка снимков в потенциальный рейс ----------

def _flight_key(s: FlightSnapshot) -> tuple:
    """Ключ, идентифицирующий физический борт.

    Идентифицируем рейс по (аэропорт, дата, время по расписанию,
    направление). Номера рейсов сюда НЕ входят: они являются атрибутом
    борта (могут быть кодшеринги), а не идентификатором, и иногда
    Яндекс отдаёт в одной ячейке всю серию номеров SU 82XX — если
    положить frozenset(flight_numbers) в ключ, один реальный борт
    размножится на десятки фантомных групп. Проверено на инциденте
    SU 8259/HZ 8259 Челябинск 28.05.2026 — 79 фантомных строк.
    """
    return (
        s.airport,
        s.flight_date,
        s.scheduled_time,
        s.destination.strip().lower(),
    )


def _last_known(
    snapshots: list[FlightSnapshot], attr: str,
    prefer_statuses: set[ParsedStatus] | None = None,
) -> str | None:
    """Последнее непустое значение поля среди снимков.

    Если задан prefer_statuses, сначала ищем среди снимков с этими
    статусами (например, гейт лучше брать из «посадка» / «регистрация»,
    а не из последнего «вылетел», где он уже пустой).
    """
    sorted_snaps = sorted(snapshots, key=lambda x: x.snapshot_at)
    if prefer_statuses:
        for s in reversed(sorted_snaps):
            if s.parsed_status in prefer_statuses:
                v = getattr(s, attr)
                if v:
                    return v
    for s in reversed(sorted_snaps):
        v = getattr(s, attr)
        if v:
            return v
    return None


def _final_status(snapshots: list[FlightSnapshot]) -> ParsedStatus:
    """Финальный статус группы — по последнему по времени снимку.

    Если последний — DEPARTED, считаем рейс вылетевшим.
    Если последний — CANCELLED или DELAYED_NEXT_DAY, рейс не учитываем.
    """
    last = max(snapshots, key=lambda x: x.snapshot_at)
    return last.parsed_status


def _collect_airlines_and_numbers(snapshots: list[FlightSnapshot]) -> tuple[
    list[str], list[str]
]:
    """Собрать все увиденные авиакомпании и номера рейсов по группе."""
    airlines: list[str] = []
    numbers: list[str] = []
    for s in snapshots:
        for a in s.airlines:
            if a not in airlines:
                airlines.append(a)
        for n in s.flight_numbers:
            if n not in numbers:
                numbers.append(n)
    return airlines, numbers


def build_daily_flights(
    snapshots: Iterable[FlightSnapshot],
    target_date: date,
) -> list[FlightDaily]:
    """Свести снимки за сутки в список финальных рейсов.

    Стадии:
      1. Фильтруем по target_date (берём только рейсы с flight_date == target_date).
      2. Группируем снимки по _flight_key.
      3. Для каждой группы определяем финальный статус.
      4. Оставляем только вылетевшие (или те, что были замечены как пропавшие
         из табло после статуса посадка/регистрация — это эвристика «вылетел»,
         помечаем review=true).
      5. Дедуплицируем кодшеринги по (gate, destination, время±2мин).
    """
    by_key: dict[tuple, list[FlightSnapshot]] = defaultdict(list)
    for s in snapshots:
        if s.flight_date != target_date:
            continue
        by_key[_flight_key(s)].append(s)

    candidates: list[FlightDaily] = []
    for key, group in by_key.items():
        status = _final_status(group)

        if status == ParsedStatus.CANCELLED:
            continue
        if status == ParsedStatus.DELAYED_NEXT_DAY:
            continue

        review = False
        review_reason: str | None = None

        if status == ParsedStatus.DEPARTED:
            pass  # норма
        elif status in {
            ParsedStatus.BOARDING, ParsedStatus.REGISTRATION,
        }:
            # Рейс перестали показывать на табло, но в последнем известном
            # снимке он был в активной фазе (посадка/регистрация). Это норма:
            # между тиками поллера рейс ушёл из 2-часового окна табло.
            # Гейт у такого рейса как правило уже зафиксирован.
            # Если гейт есть — это однозначно вылетевший, review не ставим.
            # Если гейта нет — оставляем review для ручной проверки.
            pass  # ниже проверка по гейту проставит review при необходимости
        elif status == ParsedStatus.SCHEDULED:
            # Видели только «Вылет по расписанию», статус активной фазы не
            # успели застать — менее уверены, ставим review.
            review = True
            review_reason = f"исчез из табло на статусе {status.value}"
        elif status == ParsedStatus.DELAYED_SAME_DAY:
            # Задержан, но в течение суток. Если мы не дождались DEPARTED —
            # ставим review (мог уйти после последнего тика).
            review = True
            review_reason = "задержан в пределах суток, итогового статуса не видели"
        else:
            review = True
            review_reason = f"неизвестный финальный статус: {status.value}"

        gate = _last_known(
            group, "gate",
            prefer_statuses={
                ParsedStatus.BOARDING,
                ParsedStatus.REGISTRATION,
                ParsedStatus.SCHEDULED,
                ParsedStatus.DELAYED_SAME_DAY,
            },
        )
        terminal = _last_known(group, "terminal")
        if not gate:
            review = True
            review_reason = (review_reason + "; " if review_reason else "") + \
                "гейт ни в одном снимке не был зафиксирован"

        airlines, numbers = _collect_airlines_and_numbers(group)
        airport, flight_date, scheduled_time, destination = key

        # Восстановим оригинальный destination из снимков (не lowercased).
        original_destination = group[0].destination

        candidates.append(FlightDaily(
            airport=airport,
            flight_date=flight_date,
            scheduled_time=scheduled_time,
            actual_time=None,  # rasp.yandex.ru не отдаёт фактическое время,
                               # только статус «Вылетел». Если когда-нибудь
                               # подключим второй источник — обновим.
            terminal=terminal,
            gate=gate,
            airlines=airlines,
            flight_numbers=numbers,
            destination=original_destination,
            snapshots_seen=len(group),
            review=review,
            review_reason=review_reason,
        ))

    # Дедуплицируем кодшеринги.
    merged = _merge_codeshares(candidates)
    log.info(
        "Свёртка: %d сырых групп -> %d рейсов после склейки кодшерингов",
        len(candidates), len(merged),
    )
    return merged


# ---------- склейка кодшерингов ----------

def _times_close(a: time, b: time, tol_minutes: int = 2) -> bool:
    """Время вылета совпадает в пределах ±tol минут."""
    am = a.hour * 60 + a.minute
    bm = b.hour * 60 + b.minute
    return abs(am - bm) <= tol_minutes


def _merge_codeshares(flights: list[FlightDaily]) -> list[FlightDaily]:
    """Склеить кодшеринги.

    Кодшеринг = одинаковое (airport, flight_date, destination, gate)
    и scheduled_time различается на ≤ 2 минут. Гейт не пустой обязательно —
    иначе мы рискуем склеить рейсы разных физических бортов.
    """
    if not flights:
        return []

    # Сортируем для детерминированности.
    flights_sorted = sorted(
        flights,
        key=lambda f: (f.airport, f.flight_date, f.destination,
                       f.gate or "", f.scheduled_time),
    )

    out: list[FlightDaily] = []
    consumed: set[int] = set()

    for i, base in enumerate(flights_sorted):
        if i in consumed:
            continue
        merge_targets: list[int] = [i]
        if base.gate:
            for j in range(i + 1, len(flights_sorted)):
                if j in consumed:
                    continue
                cand = flights_sorted[j]
                if (
                    cand.airport == base.airport
                    and cand.flight_date == base.flight_date
                    and cand.destination.strip().lower() ==
                        base.destination.strip().lower()
                    and cand.gate == base.gate
                    and _times_close(cand.scheduled_time, base.scheduled_time)
                ):
                    merge_targets.append(j)

        if len(merge_targets) == 1:
            out.append(base)
            consumed.add(i)
            continue

        # Сливаем
        airlines: list[str] = []
        numbers: list[str] = []
        snapshots_seen = 0
        review = False
        reasons: list[str] = []
        for k in merge_targets:
            f = flights_sorted[k]
            for a in f.airlines:
                if a not in airlines:
                    airlines.append(a)
            for n in f.flight_numbers:
                if n not in numbers:
                    numbers.append(n)
            snapshots_seen += f.snapshots_seen
            if f.review:
                review = True
                if f.review_reason:
                    reasons.append(f.review_reason)
            consumed.add(k)
        out.append(FlightDaily(
            airport=base.airport,
            flight_date=base.flight_date,
            scheduled_time=base.scheduled_time,
            actual_time=None,
            terminal=base.terminal,
            gate=base.gate,
            airlines=airlines,
            flight_numbers=numbers,
            destination=base.destination,
            snapshots_seen=snapshots_seen,
            review=review,
            review_reason="; ".join(sorted(set(reasons))) if reasons else None,
        ))

    return out
