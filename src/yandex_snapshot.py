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

Запуск: python -m src.yandex_snapshot [YYYY-MM-DD ...]
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


def _mature(airport: str, day: date) -> bool:
    st = yf.load_snapshot(airport, day)
    if st is None:
        return False
    try:
        at = datetime.strptime(st.get("_fetched_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    # Конец суток D по МСК = D+1 00:00 МСК = D 21:00 UTC; нужна ещё одна ночь.
    return at >= datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=21)


def plan(target: date, today: date) -> list[tuple[str, date]]:
    jobs = [(ap, target) for ap in AIRPORTS]
    start = max(BACKFILL_FROM, today - timedelta(days=ARCHIVE_DAYS))
    extra = []
    d = start
    while d < target:
        for ap in AIRPORTS:
            if not _mature(ap, d):
                extra.append((ap, d))
        d += timedelta(days=1)
    return jobs + extra[:MAX_BACKFILL]


def main(argv: list[str]) -> int:
    target = day_before_yesterday_msk()
    if argv:
        jobs = [(ap, date.fromisoformat(a)) for a in argv for ap in AIRPORTS]
    else:
        jobs = plan(target, target + timedelta(days=2))
    log.info("Снимаю %d страниц: %s", len(jobs),
             ", ".join("%s %s" % (ap, d) for ap, d in jobs))
    ok, bad = [], []
    with yf.make_client() as client:
        for ap, d in jobs:
            try:
                st = yf.fetch_station(ap, d, client, use_snapshot=False)
                flights = yf.flights_for_day(ap, st, d)
                if len(flights) < yf.MIN_FLIGHTS:
                    raise yf.YandexError("всего %d рейсов, страница неполная" % len(flights))
                yf.save_snapshot(ap, d, st)
                ok.append((ap, d))
                log.info("[%s] %s: снимок сохранён, %d рейсов", ap, d, len(flights))
            except yf.YandexError as e:
                bad.append((ap, d))
                log.error("[%s] %s: %s", ap, d, e)
    print("SNAPSHOT ok=%d fail=%d" % (len(ok), len(bad)))
    for ap, d in bad:
        print("FAIL %s %s" % (ap, d))
    target_days = {d for _, d in jobs[:len(AIRPORTS)]}
    return 1 if any(d in target_days for _, d in bad) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
