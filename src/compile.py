"""Сборка итогов за прошедшие сутки.

Запускается из GitHub Actions один раз в день, обычно в 09:00 MSK
(06:00 UTC). Читает все снимки за вчерашний день и день перед ним
(на случай, если рейс с расписания 23:50 был зафиксирован уже после
полуночи), сводит их в финальные записи и пишет CSV + аудит.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from src.dedupe import build_daily_flights
from src.storage import iter_snapshots_for_day, write_audit, write_daily_csv
from src.utils import get_logger, yesterday_msk
from src.validate import build_audit

log = get_logger("compile")


def compile_day(target: date) -> tuple[int, str]:
    """Собрать дневную CSV.

    Возвращает (число итоговых рейсов, путь к CSV).
    """
    # Берём снимки за сам день и за следующий день (буфер для рейсов,
    # которые улетели сразу после полуночи).
    snapshots = list(iter_snapshots_for_day(target))
    snapshots += list(iter_snapshots_for_day(target + timedelta(days=1)))
    log.info("Прочитано %d сырых снимков для дат %s и %s",
             len(snapshots), target, target + timedelta(days=1))

    daily = build_daily_flights(snapshots, target_date=target)
    log.info("Финальных рейсов: %d", len(daily))

    csv_path = write_daily_csv(target, daily)
    audit = build_audit(snapshots, daily, target)
    audit_path = write_audit(target, audit)

    log.info("CSV: %s", csv_path)
    log.info("Audit: %s", audit_path)
    return len(daily), str(csv_path)


def parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Сборка дневной CSV по аэропортам")
    p.add_argument(
        "--date", help="Дата в формате YYYY-MM-DD (по умолчанию — вчера MSK)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_cli()
    if args.date:
        from datetime import date as _date
        y, m, d = map(int, args.date.split("-"))
        target = _date(y, m, d)
    else:
        target = yesterday_msk()
    log.info("Собираю день: %s", target)
    n, path = compile_day(target)
    log.info("Готово: %d рейсов в %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
