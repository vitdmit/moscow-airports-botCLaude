"""Дополнительный источник рейсов DME — исторический снимок табло Яндекс.Расписания.

Проблема: AeroDataBox пропускает ~8-9 рейсов/день по Домодедово (мелкие
российские перевозчики: ЮТэйр-региональные, НордСтар-чартеры, Ижавиа,
Yemen Airlines и др., которых нет в FIDS-базе AeroDataBox).

Решение: дополнительно забираем страницу
  https://rasp.yandex.ru/station/9600216/?event=departure&date=YYYY-MM-DD
и добавляем в CSV рейсы со статусом «Вылетел», которых нет в AeroDataBox.

ТОЛЬКО для DME — для SVO и VKO AeroDataBox покрывает данные полнее, чем
Яндекс (379 vs ~165 у SVO; 160 vs ~130 у VKO за 17.06.2026).

Технические детали:
- Страница ?date=D показывает «доску», которая начинается с вечера дня D-1.
  Строки с явной датой «NN ммм» (напр. «16 июня») фильтруем как предыдущий день.
  Строки без явной даты — рейсы дня D.
- Дополнительная проверка: в URL ссылки на рейс есть when=YYYY-MM-DD.
- При любой ошибке (сеть, парсинг, изменение разметки) функция возвращает []
  и не роняет основной сбор.
- Гейт/терминал для дополненных рейсов берётся из снапшота Яндекс-табло
  (если снапшот за этот день есть и рейс там найден).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

import httpx

from src.config import BROWSER_HEADERS, REQUEST_TIMEOUT_SEC
from src.utils import get_logger

log = get_logger("yandex_departures")

# Яндекс.Расписания: страница табло Домодедово (вылет)
DME_STATION_URL = "https://rasp.yandex.ru/station/9600216/"

# Месяцы на русском -> номер
_MONTHS_RU: dict[str, int] = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_MONTHS_RU_INV: dict[int, str] = {v: k for k, v in _MONTHS_RU.items()}

# Регулярки
_TIME_RE = re.compile(r"\|\s+(\d{2}:\d{2})")
_FLIGHT_URL_RE = re.compile(r"flights/([A-Z0-9]{2,3})-(\d{1,4})/")
_FLIGHT_TEXT_RE = re.compile(r"\b([A-Z0-9]{2,3})\s+(\d{3,4})\b")
_WHEN_RE = re.compile(r"when=(\d{4}-\d{2}-\d{2})")
_DEST_RE = re.compile(
    r"\|\s+\d{2}:\d{2}[^|]*\|\s+([А-Яа-яёЁA-Za-z][^|\[\]\n]{2,40}?)\s+\|"
)
_AIRLINE_RE = re.compile(
    r"(?:[A-Z0-9]{2,3}[-\s]\d{1,4})[/\)?!\]\s]+([А-ЯA-Za-zа-яёЁ][^|\|<\[]{2,40}?)\s*\|\s*Вылетел"
)
# Паттерн явной даты в строке: «16 июня», «5 июля»
_EXPLICIT_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[яе]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\b"
)


def _norm(s: str) -> str:
    """Нормализовать номер рейса для сравнения: 'S7  67' -> 'S767', 'S7 67' -> 'S767'."""
    return re.sub(r"\s+", "", s.strip().upper())


def _prev_day_label(target_day: date) -> Optional[str]:
    """Строковая метка предыдущего дня: «16 июня»."""
    if target_day.day == 1:
        # первое число — предыдущий день в прошлом месяце, сложнее
        # просто пропускаем; ошибок будет мало
        return None
    m = _MONTHS_RU_INV.get(target_day.month)
    return f"{target_day.day - 1} {m}" if m else None


def _is_prev_day(line: str, target_day: date) -> bool:
    """Строка содержит явную дату, не совпадающую с target_day."""
    m = _EXPLICIT_DATE_RE.search(line)
    if not m:
        return False
    day_n = int(m.group(1))
    month_kw = m.group(2).lower()
    month_n = next(
        (v for k, v in _MONTHS_RU.items() if k.startswith(month_kw[:4])), 0
    )
    if not month_n:
        return True  # неизвестный месяц — лучше пропустим
    try:
        row_date = date(target_day.year, month_n, day_n)
        return row_date != target_day
    except ValueError:
        return True


def _parse_page(text: str, target_day: str) -> list[dict]:
    """Распарсить текст страницы Яндекс.Расписания и вернуть вылетевшие рейсы.

    text — результат web-fetch (HTML конвертирован в текст/markdown).
    target_day — строка 'YYYY-MM-DD'. Возвращает список:
      {flight_number, scheduled_time, destination, airlines}
    """
    from datetime import date as _date
    try:
        tday = _date.fromisoformat(target_day)
    except ValueError:
        return []

    results: list[dict] = []
    for line in text.split("\n"):
        # Только строки с «Вылетел»
        if "| Вылетел" not in line and "Вылетел" not in line:
            continue
        # Пропускаем строки с явной датой предыдущего дня
        if _is_prev_day(line, tday):
            continue
        # Доп. проверка по when= в URL
        wm = _WHEN_RE.search(line)
        if wm and wm.group(1) != target_day:
            continue

        # Время
        tm = _TIME_RE.search(line)
        if not tm:
            continue
        sched_time = tm.group(1)

        # Номер рейса: сначала из URL, потом из текста
        fn_m = _FLIGHT_URL_RE.search(line)
        if fn_m:
            flight_number = f"{fn_m.group(1)} {fn_m.group(2)}"
        else:
            fn2 = _FLIGHT_TEXT_RE.search(line)
            if not fn2:
                continue
            flight_number = f"{fn2.group(1)} {fn2.group(2)}"

        # Направление
        dest_m = _DEST_RE.search(line)
        destination = dest_m.group(1).strip() if dest_m else ""

        # Авиакомпания
        al_m = _AIRLINE_RE.search(line)
        airlines = al_m.group(1).strip() if al_m else ""

        results.append({
            "flight_number": flight_number,
            "scheduled_time": sched_time,
            "destination": destination,
            "airlines": airlines,
        })

    return results


def fetch_dme_departed(day: date,
                       client: Optional[httpx.Client] = None) -> list[dict]:
    """Скачать и разобрать вылетевшие рейсы DME за `day` из Яндекс.Расписания.

    Возвращает список [{flight_number, scheduled_time, destination, airlines}].
    Только рейсы со статусом «Вылетел», принадлежащие именно `day`.
    При любой ошибке возвращает [] (не роняет основной сбор).
    """
    url = f"{DME_STATION_URL}?event=departure&date={day.isoformat()}"
    try:
        own = client is None
        if own:
            client = httpx.Client(
                timeout=REQUEST_TIMEOUT_SEC,
                headers=BROWSER_HEADERS,
                follow_redirects=True,
            )
        try:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text
        finally:
            if own:
                client.close()
    except Exception as e:
        log.warning("[DME] Яндекс.Расписания: не удалось скачать за %s: %s", day, e)
        return []

    try:
        flights = _parse_page(text, day.isoformat())
        log.info("[DME] Яндекс.Расписания за %s: %d рейсов со статусом Вылетел",
                 day, len(flights))
        return flights
    except Exception as e:
        log.warning("[DME] Яндекс.Расписания: ошибка парсинга за %s: %s", day, e)
        return []


def supplement_dme(existing_rows: list[dict], day: date,
                   client: Optional[httpx.Client] = None) -> list[dict]:
    """Дополнить список рейсов DME из Яндекс.Расписания.

    existing_rows — уже собранные строки (из AeroDataBox, только DME).
    day — целевая дата.
    Возвращает НОВЫЕ строки для рейсов, которых нет в existing_rows.
    При ошибке возвращает [].

    Новые строки имеют формат CSV-файла ежедневных данных:
      airport, flight_date, scheduled_time, actual_time,
      terminal, gate, airlines, flight_numbers,
      destination, destination_iata
    Поля actual_time, terminal, gate, destination_iata могут быть пустыми.
    Если рейс найден в снапшоте Яндекс-табло — gate/terminal подставляются.
    """
    # Нормализованные номера рейсов, уже собранных AeroDataBox
    existing_nums: set[str] = set()
    for r in existing_rows:
        for num in str(r.get("flight_numbers", "")).split(","):
            n = _norm(num)
            if n:
                existing_nums.add(n)

    yandex_flights = fetch_dme_departed(day, client=client)
    if not yandex_flights:
        return []

    # Снапшот гейтов для обогащения новых рейсов
    snap: dict = {}
    try:
        from src.yandex_board import load_snapshot
        snap = load_snapshot(day)
    except Exception:
        pass

    snap_by_flight: dict[str, dict] = {}
    for v in snap.values():
        fn = _norm(str(v.get("flight", "")))
        if fn:
            snap_by_flight[fn] = v

    new_rows: list[dict] = []
    for f in yandex_flights:
        fn_norm = _norm(f["flight_number"])
        if fn_norm in existing_nums:
            continue  # уже есть в AeroDataBox

        # Гейт/терминал из снапшота
        gate = terminal = ""
        snap_hit = snap_by_flight.get(fn_norm)
        if snap_hit:
            gate = snap_hit.get("gate", "")
            terminal = snap_hit.get("terminal", "")

        new_rows.append({
            "airport": "DME",
            "flight_date": day.isoformat(),
            "scheduled_time": f["scheduled_time"],
            "actual_time": "",
            "terminal": terminal,
            "gate": gate,
            "airlines": f["airlines"],
            "flight_numbers": f["flight_number"],
            "destination": f["destination"],
            "destination_iata": "",
        })

    if new_rows:
        log.info(
            "[DME] Яндекс добавил %d рейсов за %s (не было в AeroDataBox): %s",
            len(new_rows), day,
            [r["flight_numbers"] for r in new_rows]
        )
    return new_rows
