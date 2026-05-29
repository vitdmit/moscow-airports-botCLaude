"""Опрос табло. Запускается из GitHub Actions каждые 10 минут.

Скачивает HTML табло вылета по каждому из трёх аэропортов, парсит
строки и сохраняет в JSONL-снапшот.
"""
from __future__ import annotations

import sys
import time as time_module
from datetime import datetime, timezone

import httpx

from src.config import AIRPORTS, REQUEST_TIMEOUT_SEC, USER_AGENT
from src.parser import fetch_tablo, parse_tablo
from src.storage import write_raw_html, write_snapshot
from src.utils import get_logger

log = get_logger("poll")


def poll_one(client: httpx.Client, airport: str, station_id: str) -> int:
    """Один опрос: скачать табло, распарсить, записать снапшот.

    Возвращает количество записанных строк (0 — значит что-то пошло не так,
    но это не падение — другие аэропорты опрашиваем дальше).
    """
    ts = datetime.now(tz=timezone.utc)
    try:
        html_text = fetch_tablo(station_id, client=client)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        log.error("[%s] fetch failed: %s", airport, e)
        return 0

    # Сохраняем raw HTML ДО парсинга — пригодится, если парсер упадёт
    # или потом захотим перепарсить с новой логикой.
    try:
        write_raw_html(airport, ts, html_text)
    except Exception as e:
        log.warning("[%s] не смогли сохранить raw HTML: %s", airport, e)

    try:
        rows = parse_tablo(html_text, airport=airport, snapshot_at=ts)
    except Exception as e:  # парсер не должен валиться, но подстрахуемся
        log.exception("[%s] parser raised: %s", airport, e)
        return 0

    if not rows:
        log.warning("[%s] парсер вернул 0 строк", airport)
        return 0

    path = write_snapshot(airport, ts, rows)
    log.info("[%s] записал %d строк в %s", airport, len(rows), path)
    return len(rows)


def main() -> int:
    headers = {"User-Agent": USER_AGENT}
    total = 0
    failed: list[str] = []
    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC, headers=headers) as client:
        for airport, meta in AIRPORTS.items():
            n = poll_one(client, airport, meta["station_id"])
            total += n
            if n == 0:
                failed.append(airport)
            # Вежливая пауза между запросами к одному и тому же хосту.
            time_module.sleep(2)

    log.info("Итого записано %d строк, неудачных аэропортов: %s",
             total, failed or "нет")
    # Возвращаем 0, даже если один аэропорт упал: иначе GitHub Actions
    # будет ругаться, а у нас цель — не потерять остальные данные.
    return 0


if __name__ == "__main__":
    sys.exit(main())
