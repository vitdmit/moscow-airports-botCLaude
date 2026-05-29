"""Парсер табло Яндекс.Расписаний.

ИСТОЧНИК: https://rasp.yandex.ru/station/{station_id}/?type=tablo&event=departure

Структура страницы: одна большая таблица с колонками
    Время | Направление | (значок) | Рейс | Терминал | Статус

Особенности, на которые нужно закладывать поведение:
 - Кодшеринги уже слиты в одной ячейке: «FV 6519, SU 6519 Россия, Аэрофлот».
 - У вылетевших рейсов в колонке «Статус» написано просто «Вылетел» — гейт
   пропадает. Поэтому гейт ловим у предстоящих рейсов и копим из снапшотов.
 - Статус «Вылет задержан до HH:MM» — задержка в пределах суток.
 - Статус «Вылет задержан до HH:MM N мес» — задержка на следующие сутки.
 - У некоторых рейсов нет терминала (DME часто).
 - Иногда ячейка статуса содержит несколько строк («Стойки регистрации …
   Выход на посадку …»). Бывают и multi-row trки c доп. информацией.

Чтобы не зависеть от точных CSS-классов (Яндекс может их поменять без
предупреждения), парсим текстовое содержимое таблицы через lxml/pandas:
ищем таблицу по заголовку «Время», обходим её строки и нормализуем.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

import httpx
from lxml import html as lxml_html

from src.config import (
    MSK,
    REQUEST_TIMEOUT_SEC,
    USER_AGENT,
    YANDEX_RASP_URL,
)
from src.models import FlightSnapshot, ParsedStatus
from src.utils import get_logger

log = get_logger(__name__)

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_GATE_RE = re.compile(r"Выход\s+на\s+посадку\s+([A-Za-zА-Яа-я0-9\-/]+)", re.I)
_DELAY_TIME_RE = re.compile(
    r"задерж[аи]н(?:\s+вылет)?\s*(?:до)?\s+"
    r"(\d{1,2}):(\d{2})"
    r"(?:\s+\d{1,2}\s+[а-яa-z]+)?",  # необязательный «4 апр»
    re.I,
)
_MONTH_TOKENS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def fetch_tablo(station_id: str, client: Optional[httpx.Client] = None) -> str:
    """Скачать HTML табло вылета по station_id.

    Если клиент не передан — создаём временный.
    """
    url = YANDEX_RASP_URL.format(station_id=station_id)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    if client is None:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SEC, headers=headers) as c:
            r = c.get(url, follow_redirects=True)
    else:
        r = client.get(url, headers=headers, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _find_departures_table(root) -> Optional[object]:
    """Найти таблицу табло на странице.

    Ориентируемся на заголовок «Время» в первом столбце и наличие
    столбцов «Направление», «Рейс», «Статус». Это устойчиво к смене
    CSS-классов Яндекса.
    """
    for tbl in root.xpath("//table"):
        header_text = " ".join(
            (th.text_content() or "").strip().lower()
            for th in tbl.xpath(".//thead//th | .//tr[1]//th | .//tr[1]//td")
        )
        if "время" in header_text and "направление" in header_text and (
            "статус" in header_text or "рейс" in header_text
        ):
            return tbl
    return None


def _cell_text(td) -> str:
    """Текст ячейки, схлопнутый в одну строку с пробелами."""
    raw = td.text_content() or ""
    return re.sub(r"\s+", " ", raw).strip()


def _classify_status(raw_status: str, scheduled: datetime) -> tuple[
    ParsedStatus, Optional[datetime]
]:
    """Превратить текст статуса в нормализованный enum + время задержки.

    scheduled — дата+время рейса по расписанию в MSK; используется,
    чтобы определить, не «уехала» ли задержка на следующие сутки.
    """
    text = raw_status.lower()

    if "отмен" in text:
        return ParsedStatus.CANCELLED, None

    if "вылетел" in text:
        return ParsedStatus.DEPARTED, None

    if "задерж" in text:
        m = _DELAY_TIME_RE.search(text)
        if not m:
            return ParsedStatus.DELAYED_SAME_DAY, None
        hh, mm = int(m.group(1)), int(m.group(2))
        # Определяем дату задержки.
        # Если в тексте упомянут другой день — берём его.
        # Простая эвристика: если в строке есть месяц-токен, считаем, что
        # задержка перешла на следующие сутки.
        next_day = any(tok in text for tok in _MONTH_TOKENS)
        # А ещё проверим по часам: если по расписанию рейс был, например,
        # в 23:50, а задержан «до 01:30» — это явно следующие сутки.
        delayed_day = scheduled.date()
        if next_day or (hh * 60 + mm) < (scheduled.hour * 60 + scheduled.minute):
            delayed_day = scheduled.date() + timedelta(days=1)
        delayed_dt = datetime(
            delayed_day.year, delayed_day.month, delayed_day.day,
            hh, mm, tzinfo=MSK,
        )
        if delayed_dt.date() != scheduled.date():
            return ParsedStatus.DELAYED_NEXT_DAY, delayed_dt
        return ParsedStatus.DELAYED_SAME_DAY, delayed_dt

    if "посадк" in text or "boarding" in text:
        return ParsedStatus.BOARDING, None
    if "регистрац" in text:
        return ParsedStatus.REGISTRATION, None
    if "расписан" in text:
        return ParsedStatus.SCHEDULED, None
    # Незнакомый статус — это сигнал, что Яндекс выдал что-то новое.
    # Логируем, чтобы потом расширить классификатор.
    if raw_status.strip():
        log.warning("неизвестный статус (raw): %r", raw_status[:200])
    return ParsedStatus.UNKNOWN, None


def _extract_gate(raw_status: str) -> Optional[str]:
    """Вытащить номер гейта из строки статуса, если он там есть."""
    m = _GATE_RE.search(raw_status)
    return m.group(1).strip() if m else None


# Номер рейса из href ссылки: /avia/flights/U6-261/ -> ("U6", "261")
_FLIGHT_FROM_HREF = re.compile(r"/avia/flights/([A-Z0-9]+)-(\d+[A-Z]?)/", re.I)

# Инлайновый номер рейса в тексте.
# Хитрость с (?![A-Za-z0-9]) после необязательной буквы: не даём «съесть»
# первую букву кода авиакомпании, когда номер слипся с названием,
# например «S7 4263S7 Airlines» -> номер «S7 4263», а не «S7 4263S».
_FLIGHT_INLINE = re.compile(
    r"\b([A-Z0-9]{2,3})\s+(\d{1,4}(?:[A-Z](?![A-Za-z0-9]))?)"
)


def _extract_flights_from_cell(td) -> tuple[list[str], list[str]]:
    """Разобрать ячейку «Рейс» из DOM-элемента <td>.

    Работаем с самим элементом, а не с его текстом, потому что у разных
    аэропортов номер рейса и название авиакомпании в HTML стоят вплотную
    без пробела (например «S7 3745S7 Airlines»), и склейка text_content()
    делает их неразделимыми. Зато номера рейсов почти всегда лежат либо
    в ссылках <a href=".../avia/flights/U6-261/">, либо разделимы regex-ом
    с защитой от поедания кода авиакомпании.

    Формат «FV 6519, SU 6519 Россия, Аэрофлот»
        -> (["FV 6519", "SU 6519"], ["Россия", "Аэрофлот"])
    Формат «S7 3745S7 Airlines» (слипшийся, DME)
        -> (["S7 3745"], ["S7 Airlines"])
    """
    import copy

    flights: list[str] = []

    def _add(code: str, num: str) -> None:
        f = f"{code.upper()} {num}"
        if f not in flights:
            flights.append(f)

    # 1) Номера из ссылок: сначала по тексту ссылки (там может быть
    #    несколько номеров кодшеринга), при неудаче — из href.
    for a in td.xpath(".//a"):
        link_text = re.sub(r"\s+", " ", a.text_content() or "").strip()
        found = list(_FLIGHT_INLINE.finditer(link_text))
        if found:
            for m in found:
                _add(m.group(1), m.group(2))
        else:
            m = _FLIGHT_FROM_HREF.search(a.get("href", "") or "")
            if m:
                _add(m.group(1), m.group(2))

    # 2) Авиакомпании: берём текст БЕЗ ссылок.
    #    ВАЖНО: в lxml текст после </a> хранится в a.tail и при простом
    #    remove(a) теряется. Поэтому переносим tail в соседний узел.
    td_copy = copy.deepcopy(td)
    for a in td_copy.xpath(".//a"):
        parent = a.getparent()
        prev = a.getprevious()
        tail = a.tail
        if tail:
            if prev is not None:
                prev.tail = (prev.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail
        parent.remove(a)
    remaining = re.sub(r"\s+", " ", td_copy.text_content() or "").strip()

    # 2a) В остатке могут быть безссылочные номера (3F 1316, S7 4263),
    #     слипшиеся с названием. Вытаскиваем их и заменяем пробелом.
    def _pull(m) -> str:
        _add(m.group(1), m.group(2))
        return " "

    remaining = _FLIGHT_INLINE.sub(_pull, remaining)

    # 2b) Остаток — названия авиакомпаний, разделённые запятыми.
    remaining = re.sub(r"\s+", " ", remaining).strip(" ,")
    airlines = [a.strip() for a in remaining.split(",") if a.strip()]

    return flights, airlines


def _scheduled_datetime(now: datetime, hh: int, mm: int) -> datetime:
    """Собрать datetime рейса по времени HH:MM.

    Алгоритм: берём текущую MSK-дату. Если время рейса больше чем на
    14 часов в прошлом от «сейчас» — считаем, что это рейс уже завтрашнего
    дня (Яндекс на странице «сегодня» показывает в основном будущие
    + последний час прошлого). На практике это работает корректно для
    стандартного окна табло в 2 часа: ранние утренние «01:00» в полдень
    в табло не попадают, а если попали — это «сегодня».
    """
    today = now.astimezone(MSK).date()
    candidate = datetime(today.year, today.month, today.day, hh, mm, tzinfo=MSK)
    # Если разница больше +14 часов в будущее, скорее это вчера —
    # но Яндекс на странице «сегодня» вчера показывать не должен.
    # Эту ветку оставляем как safety net.
    diff_hours = (candidate - now.astimezone(MSK)).total_seconds() / 3600.0
    if diff_hours > 14:
        candidate -= timedelta(days=1)
    elif diff_hours < -14:
        candidate += timedelta(days=1)
    return candidate


def parse_tablo(
    html_text: str,
    airport: str,
    snapshot_at: datetime,
) -> list[FlightSnapshot]:
    """Разобрать HTML страницы табло в список снимков рейсов.

    Возвращаемые объекты ещё не дедуплицированы и не отфильтрованы —
    это «сырые» наблюдения для одного тика поллера.
    """
    root = lxml_html.fromstring(html_text)
    tbl = _find_departures_table(root)
    if tbl is None:
        log.warning("[%s] не нашли таблицу табло — возможно, HTML изменился", airport)
        return []

    # Определяем порядок колонок по заголовку.
    header_cells = tbl.xpath(".//thead//th") or tbl.xpath(".//tr[1]//th") or \
        tbl.xpath(".//tr[1]//td")
    col_index: dict[str, int] = {}
    for i, th in enumerate(header_cells):
        name = (th.text_content() or "").strip().lower()
        if not name:
            continue
        if "врем" in name:
            col_index["time"] = i
        elif "направ" in name:
            col_index["destination"] = i
        elif "рейс" in name:
            col_index["flight"] = i
        elif "термин" in name:
            col_index["terminal"] = i
        elif "статус" in name:
            col_index["status"] = i

    required = {"time", "destination", "flight", "status"}
    if not required.issubset(col_index):
        log.warning(
            "[%s] таблица найдена, но не хватает колонок: было %s",
            airport, col_index,
        )
        return []

    rows: list[FlightSnapshot] = []
    # Тело таблицы. Иногда у Яндекса tbody отсутствует, идём по всем tr,
    # начиная со второй.
    body_rows = tbl.xpath(".//tbody/tr") or tbl.xpath(".//tr")[1:]

    pending_status_extra: Optional[str] = None
    for tr in body_rows:
        tds = tr.xpath("./td")
        if len(tds) < max(col_index.values()) + 1:
            # Возможно, это вторая строка с доп. статусом («Вылет задержан до …»)
            # — копим, чтобы прикрепить к следующему рейсу.
            text = _cell_text(tr)
            if text:
                pending_status_extra = text
            continue

        time_text = _cell_text(tds[col_index["time"]])
        m = _TIME_RE.match(time_text)
        if not m:
            # пустая или сервисная строка
            continue
        hh, mm = int(m.group(1)), int(m.group(2))

        destination = _cell_text(tds[col_index["destination"]])
        if not destination:
            continue

        flight_td = tds[col_index["flight"]]
        flights, airlines = _extract_flights_from_cell(flight_td)

        terminal_text = (
            _cell_text(tds[col_index["terminal"]])
            if "terminal" in col_index and col_index["terminal"] < len(tds)
            else ""
        )
        terminal = terminal_text or None

        status_text = _cell_text(tds[col_index["status"]])
        # Доп. строка статуса из предыдущей итерации (если была).
        if pending_status_extra:
            status_text = f"{status_text} {pending_status_extra}".strip()
            pending_status_extra = None

        scheduled_dt = _scheduled_datetime(snapshot_at, hh, mm)
        parsed_status, delayed_until = _classify_status(status_text, scheduled_dt)
        gate = _extract_gate(status_text)

        rows.append(FlightSnapshot(
            airport=airport,
            snapshot_at=snapshot_at,
            flight_date=scheduled_dt.date(),
            scheduled_time=time(hh, mm),
            delayed_until=delayed_until,
            terminal=terminal,
            gate=gate,
            airlines=airlines,
            flight_numbers=flights,
            destination=destination,
            raw_status=status_text,
            parsed_status=parsed_status,
        ))

    log.info("[%s] распарсили %d строк табло", airport, len(rows))
    return rows
