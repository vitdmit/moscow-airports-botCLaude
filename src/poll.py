"""Опрос табло. Запускается из GitHub Actions каждые 10 минут.

Скачивает HTML табло вылета по каждому из трёх аэропортов, парсит
строки и сохраняет в JSONL-снапшот.

Антибот-устойчивость:
 - реалистичный браузерный User-Agent (в config.BROWSER_HEADERS);
 - случайный порядок аэропортов на каждом запуске, чтобы один и тот же
   аэропорт не оказывался всегда последним в цепочке (иначе он
   систематически попадает под rate-limit — так страдал DME);
 - увеличенные и слегка рандомизированные паузы между запросами;
 - retry с растущей паузой, если Яндекс вернул заглушку без таблицы.
"""
from __future__ import annotations

import random
import sys
import time as time_module
from datetime import datetime, timezone

import httpx

from src.config import AIRPORTS, REQUEST_TIMEOUT_SEC, BROWSER_HEADERS
from src.parser import TableNotFoundError, fetch_tablo, parse_tablo
from src.storage import write_raw_html, write_snapshot
from src.utils import get_logger

log = get_logger("poll")

MAX_ATTEMPTS = 3
RETRY_BASE_SLEEP = 6


def _sleep_retry(attempt: int) -> None:
    """Пауза перед повтором: линейный рост + случайный джиттер."""
    delay = RETRY_BASE_SLEEP * attempt + random.uniform(0, 4)
    time_module.sleep(delay)


def poll_one(client: httpx.Client, airport: str, station_id: str) -> int:
    """Один опрос: скачать табло, распарсить, записать снапшот.

    При заглушке без таблицы повторяет попытку до MAX_ATTEMPTS раз.
    Возвращает количество записанных строк (0 — не получилось, но это
    не роняет опрос остальных аэропортов).
    """
    last_html: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ts = datetime.now(tz=timezone.utc)
        try:
            html_text = fetch_tablo(station_id, client=client)
            last_html = html_text
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.error("[%s] fetch failed (попытка %d/%d): %s",
                      airport, attempt, MAX_ATTEMPTS, e)
            _sleep_retry(attempt)
            continue

        try:
            rows = parse_tablo(html_text, airport=airport, snapshot_at=ts)
        except TableNotFoundError:
            log.warning("[%s] таблица не найдена (попытка %d/%d), повтор…",
                        airport, attempt, MAX_ATTEMPTS)
            _sleep_retry(attempt)
            continue
        except Exception as e:
            log.exception("[%s] parser raised: %s", airport, e)
            break

        try:
            write_raw_html(airport, ts, html_text)
        except Exception as e:
            log.warning("[%s] не смогли сохранить raw HTML: %s", airport, e)

        if not rows:
            log.info("[%s] таблица найдена, но 0 строк (пустое окно)", airport)
            return 0
        path = write_snapshot(airport, ts, rows)
        log.info("[%s] записал %d строк в %s (попытка %d)",
                 airport, len(rows), path, attempt)
        return len(rows)

    if last_html is not None:
        try:
            write_raw_html(airport, datetime.now(tz=timezone.utc), last_html)
        except Exception:
            pass
    log.error("[%s] не удалось получить таблицу за %d попыток", airport, MAX_ATTEMPTS)
    return 0


def main() -> int:
    total = 0
    failed: list[str] = []

    airports = list(AIRPORTS.items())
    random.shuffle(airports)

    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SEC, headers=BROWSER_HEADERS
    ) as client:
        for i, (airport, meta) in enumerate(airports):
            n = poll_one(client, airport, meta["station_id"])
            total += n
            if n == 0:
                failed.append(airport)
            if i < len(airports) - 1:
                time_module.sleep(random.uniform(5, 9))

    log.info("Итого записано %d строк, неудачных аэропортов: %s",
             total, failed or "нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
