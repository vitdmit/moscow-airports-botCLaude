"""Разовая конвертация history_ALL.csv -> history_ALL.parquet.

Запускать ОДИН раз (локально или в GitHub Actions), где есть pyarrow:
    pip install pandas pyarrow
    python scripts_convert_to_parquet.py

CSV истории (11.5 МБ) сожмётся в Parquet до ~1.5-2 МБ и будет читаться мгновенно.
Кладёт результат рядом: data/history/history_ALL.parquet
"""
import pandas as pd
from pathlib import Path

src = Path("data/history/history_ALL.csv")
dst = Path("data/history/history_ALL.parquet")
df = pd.read_csv(src)
df["flight_date"] = pd.to_datetime(df["flight_date"], errors="coerce")
df.to_parquet(dst, index=False)
print(f"OK: {len(df)} рейсов -> {dst} "
      f"({dst.stat().st_size/1024/1024:.1f} МБ против {src.stat().st_size/1024/1024:.1f} МБ CSV)")
