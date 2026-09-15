"""Досбор суток по снимкам Яндекса, пришедшим позже ежедневного сбора.

Если утренняя рутина опоздала или упала, daily.yml соберёт сутки из
AeroDataBox. Когда снимок появится, этот шаг пересоберёт такие сутки
(не больше MAX_DAYS за запуск, чтобы не тратить лимит AeroDataBox).
Сутки считаются собранными по Яндексу, если в data/ops_daily/<дата>.json
у каждого аэропорта в sources есть слово yandex.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

from src import yandex_fids as yf
from src.config import AIRPORTS
from src.ops_report import OPS_DIR
from src.utils import day_before_yesterday_msk, get_logger

log = get_logger("yandex_upgrade")

LOOKBACK_DAYS = 28
MAX_DAYS = 2


def needs_upgrade(d: date) -> bool:
    if not all(yf.snapshot_path(ap, d).exists() for ap in AIRPORTS):
        return False
    p = OPS_DIR / ("%s.json" % d.isoformat())
    if not p.exists():
        return True
    src = json.loads(p.read_text(encoding="utf-8")).get("sources") or {}
    return any("yandex" not in str(src.get(ap, "")) for ap in AIRPORTS)


def main() -> int:
    if os.environ.get("FETCH_DATE", "").strip():
        log.info("Ручной сбор даты, досбор по снимкам пропускаю")
        return 0
    last = day_before_yesterday_msk()
    days = [last - timedelta(days=i) for i in range(LOOKBACK_DAYS)]
    todo = [d for d in days if d >= date(2026, 9, 15) and needs_upgrade(d)]
    if not todo:
        log.info("Все сутки со снимками уже собраны по Яндексу")
        return 0
    log.info("Пересобираю по снимкам Яндекса: %s", todo[:MAX_DAYS])
    from src import daily_fetch
    rc = 0
    for d in todo[:MAX_DAYS]:
        os.environ["FETCH_DATE"] = d.isoformat()
        try:
            if daily_fetch.main() != 0:
                rc = 1
        finally:
            os.environ.pop("FETCH_DATE", None)
    return rc


if __name__ == "__main__":
    sys.exit(main())
