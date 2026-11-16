"""Снимки табло Яндекс Расписаний для сбора на GitHub.

Серверам GitHub Яндекс отдаёт капчу, поэтому страницы снимает облачная
рутина Claude (claude.ai/code/routines) каждое утро в 8:15 МСК и коммитит в
data/yandex_raw/<дата>/<аэропорт>.json.gz. Ежедневный сбор в 9:00 МСК
(daily.yml) берёт сутки из снимка, ключ и прокси не нужны.

Что снимается за запуск:
  1. позавчера по МСК (сутки, которые соберёт daily.yml), всегда заново;
  2. пропущенные сутки начиная с BACKFILL_FROM, но не старше 28 дней
     (архив Яндекса открыт на 30 дней), не больше MAX_BACKFILL страниц.
Сутки, у которых снимок сделан раньше, чем через сутки после их конца,
считаются незрелыми и снимаются заново.

С 16.11.2026 снимаются и табло прилётов (<аэропорт>_arr.json.gz): позавчера
всегда, пропуски с ARR_BACKFILL_FROM, не больше MAX_BACKFILL_ARR страниц.

Запуск: python -m src.yandex_snapshot [--arrivals] [YYYY-MM-DD ...]
  --arrivals  с датами: снять только табло прилётов (разовый добор архива).
Код выхода 1, если позавчерашние сутки снять не удалось.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from src import yandex_fids as yf
from src.config import AIRPORTS
from src.utils import day_before_yesterday_msk, get_logger

log = get_logger("yandex_snapshot")

BACKFILL_FROM = date(2026, 9, 15)   # раньше сутки уже пересобраны по Яндексу
ARCHIVE_DAYS = 28
MAX_BACKFILL = 12
ARR_BACKFILL_FROM = date(2026, 10, 17)   # глубже архив Яндекса уже закрыт
MAX_BACKFILL_ARR = 12


def _mature(airport: str, day: date, event: str = "departure") -> bool:
    st = yf.load_snapshot(airport, day, event)
    if st is None:
        return False
    try:
        at = datetime.strptime(st.get("_fetched_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    # Конец суток D по МСК = D+1 00:00 МСК = D 21:00 UTC; нужна ещё одна ночь.
    return at >= datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=21)


def _backfill(event: str, since: date, target: date, today: date,
              cap: int) -> list[tuple[str, date, str]]:
    out = []
    d = max(since, today - timedelta(days=ARCHIVE_DAYS))
    while d < target:
        for ap in AIRPORTS:
            if not _mature(ap, d, event):
                out.append((ap, d, event))
        d += timedelta(days=1)
    return out[:cap]


def plan(target: date, today: date) -> list[tuple[str, date, str]]:
    """Задания (аэропорт, сутки, табло). Первыми идут позавчерашние сутки."""
    jobs = [(ap, target, ev) for ev in yf.EVENTS for ap in AIRPORTS]
    return (jobs
            + _backfill("departure", BACKFILL_FROM, target, today, MAX_BACKFILL)
            + _backfill("arrival", ARR_BACKFILL_FROM, target, today, MAX_BACKFILL_ARR))


def main(argv: list[str]) -> int:
    target = day_before_yesterday_msk()
    events = yf.EVENTS
    if argv and argv[0] == "--arrivals":
        events, argv = ("arrival",), argv[1:]
    if argv:
        days = [date.fromisoformat(a) for a in argv]
        jobs = [(ap, d, ev) for d in days for ev in events for ap in AIRPORTS]
        target_days = set(days)
    else:
        jobs = plan(target, target + timedelta(days=2))
        target_days = {target}
    log.info("Снимаю %d страниц: %s", len(jobs),
             ", ".join("%s %s %s" % (ap, d, ev) for ap, d, ev in jobs))
    ok, bad = [], []
    with yf.make_client() as client:
        for ap, d, ev in jobs:
            try:
                st = yf.fetch_station(ap, d, client, use_snapshot=False, event=ev)
                flights = yf.flights_for_day(ap, st, d)
                if len(flights) < yf.MIN_FLIGHTS:
                    raise yf.YandexError("всего %d рейсов, страница неполная" % len(flights))
                yf.save_snapshot(ap, d, st, ev)
                ok.append((ap, d, ev))
                log.info("[%s] %s %s: снимок сохранён, %d рейсов", ap, d, ev, len(flights))
            except yf.YandexError as e:
                bad.append((ap, d, ev))
                log.error("[%s] %s %s: %s", ap, d, ev, e)
    print("SNAPSHOT ok=%d fail=%d" % (len(ok), len(bad)))
    for ap, d, ev in bad:
        print("FAIL %s %s %s" % (ap, d, ev))
    # Красным делаем только провал ВЫЛЕТОВ за целевые сутки: на них стоит
    # ежедневный сбор. Провал прилётов виден в строках FAIL и добирается
    # бэкфиллом следующих запусков.
    return 1 if any(d in target_days and ev == "departure" for _, d, ev in bad) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
