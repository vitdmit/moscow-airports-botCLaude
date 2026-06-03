"""Аналитика загрузки гейтов: динамика неделя/месяц/год из единого хранилища.

Единое хранилище = история коллег (data/history/history_ALL.parquet) + наши
ежедневные сборы (data/daily/*.csv). Один рейс = одна строка. Из этой таблицы
любая динамика считается группировкой, без ручного Excel.

МЕТОДИКА доли гейта (совпадает с методикой коллег, проверено):
  доля гейта = рейсов с гейта за период / рейсов терминала (или зоны) за период.
  Сумма долей всех гейтов терминала = 100%.

Запуск:
  python -m src.analytics --period week   --gates "117,118,119,120,121"  --airport SVO
  python -m src.analytics --period month  --airport DME --terminal E
  python -m src.analytics --compare 2025-05 2026-05 --airport VKO
"""
from __future__ import annotations

import argparse
import glob
import os
from datetime import date, datetime, timedelta

import pandas as pd

from src.config import DATA_DIR

HISTORY_DIR = DATA_DIR / "history"
DAILY_DIR = DATA_DIR / "daily"

# колонки, общие для обоих источников и нужные аналитике
CORE = ["airport", "flight_date", "terminal", "gate", "destination"]


def _load_history() -> pd.DataFrame:
    """История коллег. Ищем в data/history/ и прямо в data/ (куда могли
    загрузить через GitHub Upload). Предпочитаем parquet, иначе csv."""
    candidates = [
        HISTORY_DIR / "history_ALL.parquet",
        HISTORY_DIR / "history_ALL.csv",
        DATA_DIR / "history_ALL.parquet",
        DATA_DIR / "history_ALL.csv",
    ]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        return pd.DataFrame(columns=CORE)
    df = pd.read_parquet(src) if src.suffix == ".parquet" else pd.read_csv(src)
    # отсекаем будущие (план, не факт), если столбец есть
    if "is_future" in df.columns:
        df = df[~df["is_future"].astype(bool)]
    keep = [c for c in CORE if c in df.columns]
    df = df[keep].copy()
    df["src"] = "history"
    return df


def _load_daily() -> pd.DataFrame:
    """Наши ежедневные сборы из data/daily/*.csv."""
    files = [f for f in glob.glob(str(DAILY_DIR / "*.csv"))
             if not os.path.basename(f).startswith("_")]
    frames = []
    for f in files:
        try:
            d = pd.read_csv(f)
            keep = [c for c in CORE if c in d.columns]
            frames.append(d[keep])
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=CORE)
    df = pd.concat(frames, ignore_index=True)
    df["src"] = "daily"
    return df


def load_all() -> pd.DataFrame:
    """Единое хранилище: история + ежедневные. Дедупликация по
    (аэропорт, дата, гейт, направление, время) если время есть."""
    h = _load_history()
    d = _load_daily()
    df = pd.concat([h, d], ignore_index=True)
    df = df[df["flight_date"].notna()].copy()
    df["flight_date"] = pd.to_datetime(df["flight_date"], errors="coerce")
    df = df[df["flight_date"].notna()]
    # нормализуем гейт/терминал к строке без пробелов
    for c in ("gate", "terminal", "airport"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df


def gate_share(df: pd.DataFrame, start: date, end: date,
               airport: str, within: str = "terminal") -> pd.DataFrame:
    """Доля каждого гейта за период [start, end] внутри своего терминала.

    within: 'terminal' — доля внутри терминала; 'airport' — доля в аэропорту.
    Возвращает таблицу: gate, terminal, рейсов, доля_%.
    """
    m = df[(df.airport == airport)
           & (df.flight_date.dt.date >= start)
           & (df.flight_date.dt.date <= end)].copy()
    if m.empty:
        return pd.DataFrame(columns=["gate", "terminal", "рейсов", "доля_%"])
    by_gate = (m.groupby(["terminal", "gate"]).size()
               .reset_index(name="рейсов"))
    if within == "terminal":
        denom = by_gate.groupby("terminal")["рейсов"].transform("sum")
    else:
        denom = by_gate["рейсов"].sum()
    by_gate["доля_%"] = (by_gate["рейсов"] / denom * 100).round(2)
    return by_gate.sort_values(["terminal", "рейсов"], ascending=[True, False])


def compare_periods(df: pd.DataFrame, p1: tuple[date, date],
                    p2: tuple[date, date], airport: str) -> pd.DataFrame:
    """Сравнить загрузку гейтов между двумя периодами. Возвращает:
    gate, terminal, доля_p1, доля_p2, Δ_пп (разница долей в процентных пунктах),
    рейсов_p1, рейсов_p2."""
    a = gate_share(df, p1[0], p1[1], airport).set_index(["terminal", "gate"])
    b = gate_share(df, p2[0], p2[1], airport).set_index(["terminal", "gate"])
    out = a[["доля_%", "рейсов"]].join(
        b[["доля_%", "рейсов"]], lsuffix="_p1", rsuffix="_p2", how="outer").fillna(0)
    out["Δ_пп"] = (out["доля_%_p2"] - out["доля_%_p1"]).round(2)
    out = out.reset_index().sort_values(["terminal", "доля_%_p2"],
                                        ascending=[True, False])
    return out


def _month_bounds(ym: str) -> tuple[date, date]:
    y, m = (int(x) for x in ym.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airport", default="SVO")
    ap.add_argument("--terminal", default=None)
    ap.add_argument("--period", choices=["week", "month"], default=None,
                    help="последняя неделя/месяц от сегодня")
    ap.add_argument("--compare", nargs=2, metavar=("YYYY-MM", "YYYY-MM"),
                    help="сравнить два месяца")
    args = ap.parse_args()

    df = load_all()
    if df.empty:
        print("Хранилище пусто — положи history в data/history/ и/или собери дни.")
        return 1
    print(f"Загружено {len(df)} рейсов, "
          f"{df.flight_date.min().date()}..{df.flight_date.max().date()}")

    if args.compare:
        p1 = _month_bounds(args.compare[0])
        p2 = _month_bounds(args.compare[1])
        res = compare_periods(df, p1, p2, args.airport)
        if args.terminal:
            res = res[res.terminal == args.terminal]
        print(f"\nСравнение {args.airport}: {args.compare[0]} vs {args.compare[1]}")
        print(res.to_string(index=False))
        zero_p1 = res[(res["рейсов_p1"] == 0) & (res["рейсов_p2"] > 0)]
        if not zero_p1.empty:
            gs = ", ".join(zero_p1["gate"].astype(str).tolist())
            print(f"\n⚠ Внимание: гейты [{gs}] в периоде {args.compare[0]} = 0 рейсов. "
                  f"Скорее всего в тот период они ещё НЕ велись в исходных данных "
                  f"(0 = нет данных, а не отсутствие рейсов). Сравнение по ним "
                  f"некорректно — сопоставляй только гейты, что велись в оба периода.")
        return 0

    today = date.today()
    if args.period == "week":
        start, end = today - timedelta(days=7), today
    elif args.period == "month":
        start, end = today - timedelta(days=30), today
    else:
        end = today
        start = today - timedelta(days=7)
    res = gate_share(df, start, end, args.airport)
    if args.terminal:
        res = res[res.terminal == args.terminal]
    print(f"\nЗагрузка гейтов {args.airport} за {start}..{end}:")
    print(res.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
