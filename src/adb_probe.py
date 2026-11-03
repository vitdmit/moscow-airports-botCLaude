"""Одноразовая проба: запросить у AeroDataBox рейсы DME по номеру (список в data/adb_probe/miss.json)."""
import json, os, re, time
import httpx
from src import aerodatabox as adb

MISS = adb.USAGE_FILE.parent / "adb_probe" / "miss.json"
OUT = adb.USAGE_FILE.parent / "adb_probe" / "result.json"


def main() -> int:
    key = os.environ["AERODATABOX_KEY"]
    miss = json.loads(MISS.read_text(encoding="utf-8"))
    res, n = {}, 0
    headers = {"x-rapidapi-key": key, "x-rapidapi-host": adb.API_HOST, "Accept": "application/json"}
    with httpx.Client(timeout=30) as c:
        for day, items in miss.items():
            for num, tm in items:
                n += 1
                url = f"https://{adb.API_HOST}/flights/number/{re.sub(r'\s', '', num)}/{day}"
                params = {"dateLocalRole": "Departure", "withAircraftImage": "false", "withLocation": "false"}
                try:
                    r = c.get(url, params=params, headers=headers)
                    body = r.json() if r.status_code == 200 and r.text.strip() else None
                    res[f"{day} {num} {tm}"] = {"status": r.status_code, "body": body}
                except Exception as e:
                    res[f"{day} {num} {tm}"] = {"status": "ERR", "body": str(e)}
                time.sleep(adb.REQUEST_PAUSE_SEC)
    adb._bump_usage(n)
    OUT.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print("запросов:", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
