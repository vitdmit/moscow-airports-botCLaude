"""Проверка полноты собранных дней и автодосбор дырявых.

Логика:
  1. По data/daily/*.csv считаем медиану числа рейсов на (аэропорт, день недели)
     за последние HISTORY_WEEKS недель.
  2. Последние LOOKBACK_DAYS дней: если по аэропорту собрано < MIN_RATIO от
     медианы — день «подозрительный».
  3. До MAX_REFETCH подозрительных дней пересобираем в этом же запуске.
  4. ЕСЛИ ПЕРЕСБОР НЕ ПОМОГ (число не изменилось) — дыра в самой истории
     AeroDataBox, повторять бессмысленно. День фиксируется в
     data/completeness_exceptions.json как ИЗВЕСТНОЕ ИСКЛЮЧЕНИЕ и больше
     не пересобирается и не краснит прогон (пока его счёт не изменится).
  5. Красный прогон (флаг /tmp/completeness_flag.txt) — только для НОВЫХ
     неполных дней, которые ещё не пробовали лечить.

День, собранный вручную в этом же запуске (FETCH_DATE), повторно не
пересобирается: если он остался ниже порога — сразу уходит в исключения.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

from src.config import DAILY_DIR, DATA_DIR
from src.utils import get_logger

log = get_logger("completeness")

LOOKBACK_DAYS = 14
HISTORY_WEEKS = 8
MIN_RATIO = 0.85
MAX_REFETCH = 2
FLAG_FILE = Path("/tmp/completeness_flag.txt")
EXC_FILE = DATA_DIR / "completeness_exceptions.json"

AIRPORTS = ("SVO", "VKO", "DME")


def day_counts() -> dict[tuple[str, date], int]:
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
    hist: dict[tuple[str, int], list[int]] = {}
    lo = upto - timedelta(weeks=HISTORY_WEEKS)
    for (a, d), n in counts.items():
        if lo <= d <= upto:
            hist.setdefault((a, d.weekday()), []).append(n)
    return {k: statistics.median(v) for k, v in hist.items() if len(v) >= 3}


def load_exceptions() -> dict[str, int]:
    try:
        return json.loads(EXC_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_exceptions(exc: dict[str, int]) -> None:
    EXC_FILE.write_text(json.dumps(exc, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")


def suspicious_days(counts, meds, upto, exc):
    """[(дата, аэропорт, счёт, медиана)], исключая известные исключения."""
    bad = []
    for delta in range(LOOKBACK_DAYS):
        d = upto - timedelta(days=delta)
        if not any((a, d) in counts for a in AIRPORTS):
            continue
        for a in AIRPORTS:
            n = counts.get((a, d), 0)
            med = meds.get((a, d.weekday()))
            if not (med and n < MIN_RATIO * med):
                continue
            if exc.get(f"{d.isoformat()}|{a}") == n:
                continue   # известная дыра источника, счёт не изменился
            bad.append((d, a, n, med))
    return sorted(bad)


def refetch(d: date) -> bool:
    os.environ["FETCH_DATE"] = d.isoformat()
    try:
        from src import daily_fetch
        return daily_fetch.main() == 0
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
    exc = load_exceptions()
    bad = suspicious_days(counts, meds, upto, exc)

    if not bad:
        log.info("Полнота ОК (порог %.0f%% от медианы; известных исключений: %d)",
                 MIN_RATIO * 100, len(exc))
        return 0

    for d, a, n, med in bad:
        log.warning("НЕПОЛНЫЙ ДЕНЬ %s [%s]: %d рейсов при медиане %.0f",
                    d, a, n, med)

    # День, уже собранный вручную в этом запуске — не пересобираем повторно
    manual_raw = os.environ.get("FETCH_DATE", "").strip()
    tried: set[date] = set()
    if manual_raw:
        try:
            tried.add(date.fromisoformat(manual_raw))
        except ValueError:
            pass

    have_key = bool(os.environ.get("AERODATABOX_KEY", "").strip())
    refetched = 0
    if have_key:
        for d in dict.fromkeys(x[0] for x in bad):
            if d in tried:
                continue
            if refetched >= MAX_REFETCH:
                log.info("Лимит пересборов за запуск (%d) исчерпан", MAX_REFETCH)
                break
            log.info("Автодосбор %s ...", d)
            if refetch(d):
                refetched += 1
                tried.add(d)
    else:
        log.warning("Нет AERODATABOX_KEY — автодосбор невозможен")

    # перепроверка; пробованные и не вылечившиеся дни -> исключения
    counts2 = day_counts()
    still = suspicious_days(counts2, meds, upto, exc)
    new_flags = []
    for d, a, n, med in still:
        if d in tried:
            exc[f"{d.isoformat()}|{a}"] = n
            log.warning("Дыра в источнике: %s [%s] = %d после пересбора. "
                        "Зафиксирована как известное исключение.", d, a, n)
            print(f"::notice::Известная дыра источника: {d} [{a}] = {n} "
                  f"(медиана {med:.0f}); больше не пересобирается")
        else:
            new_flags.append((d, a, n, med))
    save_exceptions(exc)

    if new_flags:
        lines = [f"{d} [{a}]: {n} рейсов, медиана {med:.0f}"
                 for d, a, n, med in new_flags]
        FLAG_FILE.write_text("\n".join(lines), encoding="utf-8")
        for ln in lines:
            print(f"::warning::Неполный день (ещё не пересобран): {ln}")
        log.error("Неполных дней без попытки лечения: %d — пересоберутся "
                  "в следующих запусках.", len(new_flags))
    else:
        log.info("Все подозрительные дни обработаны (пересобрано: %d, "
                 "исключений всего: %d)", refetched, len(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
