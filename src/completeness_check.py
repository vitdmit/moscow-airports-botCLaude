"""Проверка полноты собранных дней и автодосбор дырявых.

Проблема, которую решает: если одно из 12-часовых окон AeroDataBox вернуло
204/урезанный ответ, день записывался НЕПОЛНЫМ без ошибки (SVO 18.06 и 22.06,
DME 02.06/04.06/20.06 — реальные случаи). Логи никто не читает — нужен
автоматический контроль.

Логика:
  1. По data/daily/*.csv считаем медиану числа рейсов на (аэропорт, день недели)
     за последние HISTORY_WEEKS недель.
  2. Последние LOOKBACK_DAYS дней: если по аэропорту собрано < MIN_RATIO от
     медианы — день «подозрительный».
  3. До MAX_REFETCH подозрительных дней пересобираем прямо в этом запуске
     (бюджет бережём: пересбор = 6 запросов/день). Гейты DME берутся из
     снапшотов в репо, они никуда не деваются.
  4. Если после пересбора подозрительные дни остались — пишем флаг
     /tmp/completeness_flag.txt; workflow увидит его и станет красным
     (придёт письмо от GitHub).

Запуск: python -m src.completeness_check  (нужен AERODATABOX_KEY для автодосбора;
без ключа — только проверка и флаг).
"""
from __future__ import annotations

import csv
import os
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

from src.config import DAILY_DIR
from src.utils import get_logger

log = get_logger("completeness")

LOOKBACK_DAYS = 14     # сколько последних дней проверяем
HISTORY_WEEKS = 8      # глубина истории для медианы
MIN_RATIO = 0.85       # ниже этой доли от медианы — подозрительно
MAX_REFETCH = 2        # максимум пересборов за один запуск (бюджет!)
FLAG_FILE = Path("/tmp/completeness_flag.txt")

AIRPORTS = ("SVO", "VKO", "DME")


def day_counts() -> dict[tuple[str, date], int]:
    """(аэропорт, дата) -> число рейсов, по всем CSV в data/daily/."""
    out: dict[tuple[str, date], int] = {}
    for p in sorted(DAILY_DIR.glob("*.csv")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        with p.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                a = row.get("airport", "")
                if a:
                    out[(a, d)] = out.get((a, d), 0) + 1
    return out


def medians(counts: dict, upto: date) -> dict[tuple[str, int], float]:
    """(аэропорт, weekday) -> медиана за HISTORY_WEEKS недель до upto."""
    hist: dict[tuple[str, int], list[int]] = {}
    lo = upto - timedelta(weeks=HISTORY_WEEKS)
    for (a, d), n in counts.items():
        if lo <= d <= upto:
            hist.setdefault((a, d.weekday()), []).append(n)
    return {k: statistics.median(v) for k, v in hist.items() if len(v) >= 3}


def suspicious_days(counts: dict, meds: dict, upto: date) -> list[tuple[date, str, int, float]]:
    """Дни за LOOKBACK_DAYS, где какой-то аэропорт заметно ниже медианы."""
    bad = []
    for delta in range(LOOKBACK_DAYS):
        d = upto - timedelta(days=delta)
        if not any((a, d) in counts for a in AIRPORTS):
            continue   # дня вообще нет (не собран) — это ловит daily_fetch
        for a in AIRPORTS:
            n = counts.get((a, d), 0)
            med = meds.get((a, d.weekday()))
            if med and n < MIN_RATIO * med:
                bad.append((d, a, n, med))
    return sorted(bad)


def refetch(d: date) -> bool:
    """Пересобрать день d через штатный daily_fetch (FETCH_DATE)."""
    os.environ["FETCH_DATE"] = d.isoformat()
    try:
        from src import daily_fetch
        rc = daily_fetch.main()
        return rc == 0
    except Exception as e:
        log.error("Пересбор %s упал: %s", d, e)
        return False
    finally:
        os.environ.pop("FETCH_DATE", None)


def main() -> int:
    FLAG_FILE.unlink(missing_ok=True)
    counts = day_counts()
    if not counts:
        log.warning("data/daily пуст — нечего проверять")
        return 0
    upto = max(d for _, d in counts)
    meds = medians(counts, upto)
    bad = suspicious_days(counts, meds, upto)

    if not bad:
        log.info("Полнота ОК: последние %d дней в норме (порог %.0f%% от медианы)",
                 LOOKBACK_DAYS, MIN_RATIO * 100)
        return 0

    for d, a, n, med in bad:
        log.warning("НЕПОЛНЫЙ ДЕНЬ %s [%s]: %d рейсов при медиане %.0f",
                    d, a, n, med)

    have_key = bool(os.environ.get("AERODATABOX_KEY", "").strip())
    refetched: set[date] = set()
    if have_key:
        for d in dict.fromkeys(x[0] for x in bad):   # уникальные даты по порядку
            if len(refetched) >= MAX_REFETCH:
                log.info("Лимит пересборов за запуск (%d) исчерпан", MAX_REFETCH)
                break
            log.info("Автодосбор %s ...", d)
            if refetch(d):
                refetched.add(d)
    else:
        log.warning("Нет AERODATABOX_KEY — автодосбор невозможен")

    # перепроверка после досбора
    counts2 = day_counts()
    still = [x for x in suspicious_days(counts2, meds, upto)]
    if still:
        lines = [f"{d} [{a}]: {n} рейсов, медиана {med:.0f}"
                 for d, a, n, med in still]
        FLAG_FILE.write_text("\n".join(lines), encoding="utf-8")
        for ln in lines:
            print(f"::warning::Неполный день: {ln}")
        log.error("Остались неполные дни: %d (см. выше). Флаг записан.", len(still))
    else:
        log.info("После автодосбора все дни в норме (пересобрано: %s)",
                 ", ".join(map(str, sorted(refetched))) or "ничего")
    return 0


if __name__ == "__main__":
    sys.exit(main())
