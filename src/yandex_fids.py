"""Основной источник вылетов: табло Яндекс Расписаний за сутки.

Страница https://rasp.yandex.ru/station/<id>/?date=YYYY-MM-DD содержит в
window.INITIAL_STATE полный список рейсов суток (station.threads): плановое
время, номер, назначение (IATA и город), статус, фактическое время, гейт,
терминал, кодшеринги. Архив открыт на 30 дней назад. Один запрос на
аэропорт в сутки, ключ не нужен.

Проверено 15.09.2026 на архиве 16.08-14.09 (21 098 строк):
  статусы в прошлых сутках только departed / cancelled / unknown;
  гейт есть у 99.7% вылетевших;
  DME 12.09: 99 вылетело, 1 отменён (AeroDataBox отдал ноль).

Кодшеринги. Яндекс сам склеивает большую часть (поле codeshares), но не
все: например, FZ 952 и EK 2176 из Внукова, YC 90 и UT 4090 из Домодедова
идут отдельными строками с одинаковым плановым временем, назначением,
гейтом и фактическим временем до минуты. Два разных борта так совпасть не
могут, поэтому такие строки склеиваются здесь (_merge_codeshares).
Первым номером идёт строка с isSupplement=false (оператор рейса).

Сутки рейса. Вылетевший рейс относится к дате ФАКТИЧЕСКОГО вылета (как и
раньше в сборе AeroDataBox). Страница за D содержит и вчерашние рейсы,
ушедшие после полуночи, и рейсы D, ушедшие уже в D+1. Первые берём,
вторые отбрасываем: они есть на странице D+1. Отменённые и рейсы без
статуса относятся к плановой дате.

Полнота. Часть перевозчиков на табло Яндекса не попадает: по DME за
16-31.08.2026 это NordStar Y7 402/407/408, Sky FRU R8 484, в отдельные
дни Belavia и Уральские авиалинии, в среднем 2-3 рейса в сутки. Поэтому
сутки дополняются рейсами AeroDataBox, которых на странице Яндекса нет
(adb_extra). По 14 сопоставимым дням августа с ручным учётом коллег (сутки
по плановой дате, как у коллег): AeroDataBox -4.8%, Яндекс -2.7%, вместе
-0.4%.

Доступ. Серверам GitHub Яндекс отдаёт капчу (проверено 15.09.2026, прогон
пересбора #7), VPS в Нидерландах тоже (капча даже на ya.ru). Поэтому
страницы снимает облачная рутина Claude (src/yandex_snapshot.py) и кладёт
в data/yandex_raw/<дата>/<аэропорт>.json.gz. Сбор на GitHub берёт сутки из
снимка. Нет снимка: на GitHub Яндекс не запрашивается вовсе (только при
заданном YANDEX_PROXY), сутки берутся из AeroDataBox. После первой капчи
модуль до конца запуска больше не стучится (_BLOCKED).

Фактическое время. У SVO оно с секундами и в среднем позже, чем
revisedTime AeroDataBox (SU 1424 12.09: 00:15:35 против 00:03), похоже на
отрыв от полосы. Поэтому сводки из этого источника пишутся схемой 3 и с
историей AeroDataBox (схема 2) не смешиваются.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time as time_module
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from src.config import AIRPORTS as AIRPORT_CONFIGS, BROWSER_HEADERS, REQUEST_TIMEOUT_SEC
from src.config import DATA_DIR
from src.utils import get_logger

log = get_logger("yandex_fids")

URL = "https://rasp.yandex.ru/station/{sid}/?date={d}"
STATE_MARK = "window.INITIAL_STATE = "

# Пауза между запросами к Яндексу. При запросах раз в 2 секунды 15 страниц
# из 90 ушли в капчу, при паузе 20 секунд капча снималась.
PAUSE_SEC = 10
RETRY_WAITS = (20, 60)

# Бизнес-терминалы: деловая авиация и корпоративные борта (Газпромавиа во
# Внукове-3, Шереметьево-A). Прежний сбор их тоже не брал (withPrivate=false).
BUSINESS_TERMINALS = {"SVO": {"A"}, "VKO": {"3", "3A"}}

# Статус unknown в прошедших сутках: ни фактического времени, ни гейта.
# Проверено 15.09.2026: из 234 таких рейсов SVO и VKO за 16.08-14.09 ни один
# не нашёлся среди вылетевших у AeroDataBox, по DME ни один из 12 за четыре
# дня не нашёлся у Flightradar24. Значит, это несостоявшиеся рейсы, в учёт
# не берём ни как вылет, ни как план.
SKIP_STATUSES = {"unknown"}

# Меньше этого за сутки по аэропорту быть не может: значит, страница
# пришла неполной, и сутки надо брать из резервного источника.
MIN_FLIGHTS = 30


class YandexError(Exception):
    pass


_last_request = 0.0
_BLOCKED = False

# Номера и пары (плановое время, назначение) со страницы последних суток,
# по аэропортам. Нужны adb_extra, чтобы не задвоить рейс, который Яндекс
# знает, но отнёс к другим суткам или записал под другим номером.
LAST_PAGE: dict[str, tuple[set, set]] = {}
MOSCOW_IATA = {"SVO", "VKO", "DME", "ZIA", "BKA", "OSF"}

SNAP_DIR = DATA_DIR / "yandex_raw"


def snapshot_path(airport: str, day: date):
    return SNAP_DIR / day.isoformat() / ("%s.json.gz" % airport)


def save_snapshot(airport: str, day: date, station: dict) -> None:
    p = snapshot_path(airport, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = dict(station)
    body["_fetched_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(p, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))


def load_snapshot(airport: str, day: date) -> Optional[dict]:
    p = snapshot_path(airport, day)
    if not p.exists():
        return None
    try:
        station = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
    except (OSError, ValueError) as e:
        log.error("[%s] %s: снимок %s не читается: %s", airport, day, p, e)
        return None
    if not isinstance(station.get("threads"), list):
        log.error("[%s] %s: в снимке нет threads", airport, day)
        return None
    return station


def _network_allowed() -> bool:
    """На серверах GitHub Яндекс отдаёт капчу, без прокси не стучимся."""
    if os.environ.get("YANDEX_PROXY", "").strip():
        return True
    return os.environ.get("GITHUB_ACTIONS", "").lower() != "true"


def make_client() -> httpx.Client:
    proxy = os.environ.get("YANDEX_PROXY", "").strip() or None
    return httpx.Client(timeout=REQUEST_TIMEOUT_SEC, headers=BROWSER_HEADERS,
                        follow_redirects=True, proxy=proxy)


def _pause():
    global _last_request
    wait = PAUSE_SEC - (time_module.monotonic() - _last_request)
    if wait > 0:
        time_module.sleep(wait)
    _last_request = time_module.monotonic()


def parse_state(html: str) -> dict:
    i = html.find(STATE_MARK)
    if i < 0:
        raise YandexError("на странице нет INITIAL_STATE (капча или новая разметка)")
    state, _ = json.JSONDecoder().raw_decode(html[i + len(STATE_MARK):])
    station = state.get("station") or {}
    if not isinstance(station.get("threads"), list):
        raise YandexError("в INITIAL_STATE нет station.threads")
    return station


def fetch_station(airport: str, day: date,
                  client: Optional[httpx.Client] = None,
                  use_snapshot: bool = True) -> dict:
    global _BLOCKED
    if use_snapshot:
        station = load_snapshot(airport, day)
        if station is not None:
            log.info("[%s] %s: табло Яндекса из снимка (%s)", airport, day,
                     station.get("_fetched_at", "?"))
            return station
        if not _network_allowed():
            raise YandexError("[%s] %s: снимка табло нет, а с GitHub Яндекс не отвечает"
                              % (airport, day))
    if _BLOCKED:
        raise YandexError("[%s] %s: Яндекс в этом запуске отдаёт капчу, не запрашиваю"
                          % (airport, day))
    sid = AIRPORT_CONFIGS[airport]["station_id"]
    url = URL.format(sid=sid, d=day.isoformat())
    own = client is None
    if own:
        client = make_client()
    try:
        last = None
        for attempt in range(len(RETRY_WAITS) + 1):
            _pause()
            try:
                r = client.get(url)
                if r.status_code != 200:
                    raise YandexError("HTTP %s" % r.status_code)
                station = parse_state(r.text)
                if station.get("iataCode") not in (None, airport):
                    raise YandexError("страница не того аэропорта: %s" % station.get("iataCode"))
                if station.get("whenDate") not in (None, day.isoformat()):
                    raise YandexError("страница не тех суток: %s" % station.get("whenDate"))
                return station
            except (YandexError, httpx.HTTPError, ValueError) as e:
                last = e
                if attempt < len(RETRY_WAITS):
                    log.warning("[%s] %s: %s, повтор через %sс", airport, day, e,
                                RETRY_WAITS[attempt])
                    time_module.sleep(RETRY_WAITS[attempt])
        if "INITIAL_STATE" in str(last):
            _BLOCKED = True
        raise YandexError("[%s] %s: страница не получена: %s" % (airport, day, last))
    finally:
        if own:
            client.close()


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _flight(t: dict, companies: dict) -> Optional[dict]:
    if t.get("transportType") not in (None, "plane"):
        return None
    sched = _dt((t.get("eventDt") or {}).get("datetime"))
    if sched is None:
        return None
    st = t.get("status") or {}
    status = (st.get("status") or "unknown").lower()
    actual = _dt(st.get("actualDt")) if status == "departed" else None
    route = t.get("routeStations") or [{}]
    first = route[0] or {}
    pairs = [(t.get("number") or "", t.get("companyId"))]
    for cs in t.get("codeshares") or []:
        pairs.append((cs.get("number") or "", cs.get("companyId")))
    pairs = [p for p in pairs if p[0]]
    return {
        "sched": sched,
        "actual": actual,
        "status": status,
        "gate": (st.get("gate") or "").strip(),
        "terminal_raw": (st.get("actualTerminalName") or t.get("terminalName") or ""),
        "numbers": [p[0] for p in pairs],
        "company_ids": [p[1] for p in pairs],
        "supplement": bool(t.get("isSupplement")),
        "dest_iata": first.get("iataCode") or "",
        "dest": first.get("settlement") or first.get("title") or "",
        "companies": companies,
    }


def _merge_key(f: dict) -> tuple:
    # У вылетевших сравниваем плановое время суток без даты: Яндекс бывает
    # датирует кодшеринг другими сутками. Пример 11.09.2026 из Внукова:
    # FZ 976 по плану 10.09 03:30 и EK 2285 по плану 11.09 03:30, оба ушли
    # 11.09 в 04:13 с гейта 29. Это один борт. Плановая дата берётся у
    # оператора (строка с isSupplement=false).
    if f["actual"] is not None:
        return (f["sched"].strftime("%H:%M"), f["dest_iata"], f["status"],
                f["actual"].strftime("%Y-%m-%dT%H:%M"))
    return (f["sched"].isoformat(), f["dest_iata"], f["status"], "")


def _merge_codeshares(flights: list[dict]) -> list[dict]:
    """Склеить строки одного борта, которые Яндекс не склеил сам."""
    groups: dict[tuple, list[dict]] = {}
    for f in flights:
        groups.setdefault(_merge_key(f), []).append(f)
    out = []
    for grp in groups.values():
        # Внутри ключа разные гейты означают разные борта: не склеиваем.
        by_gate: dict[str, list[dict]] = {}
        for f in grp:
            by_gate.setdefault(f["gate"], []).append(f)
        gated = {g: v for g, v in by_gate.items() if g}
        if len(gated) <= 1:
            # один гейт (или гейта нет): строки без гейта прилипают к нему
            clusters = [grp]
        else:
            clusters = list(gated.values())
            clusters[0] = clusters[0] + by_gate.get("", [])
        for cl in clusters:
            cl = sorted(cl, key=lambda f: (f["supplement"], f["numbers"][0] if f["numbers"] else ""))
            head = dict(cl[0])
            nums, comps = [], []
            for f in cl:
                for n, c in zip(f["numbers"], f["company_ids"]):
                    if n not in nums:
                        nums.append(n)
                        comps.append(c)
                if not head["gate"] and f["gate"]:
                    head["gate"] = f["gate"]
            # Номер оператора первым, остальные по алфавиту: порядок
            # кодшерингов на странице Яндекса от запроса к запросу плавает.
            rest = sorted(zip(nums[1:], comps[1:]), key=lambda p: p[0])
            head["numbers"] = nums[:1] + [p[0] for p in rest]
            head["company_ids"] = comps[:1] + [p[1] for p in rest]
            out.append(head)
    return out


def _terminal(airport: str, f: dict) -> str:
    t = str(f["terminal_raw"] or "").strip().upper()
    if t in ("0", "NULL", "NONE"):
        t = ""
    if airport == "DME":
        g = f["gate"].upper()
        return g[:1] if g[:1].isalpha() else ""
    if airport == "VKO":
        return "A" if t in ("", "A") else t
    return t


def _flight_day(f: dict) -> date:
    return (f["actual"] or f["sched"]).date()


def flights_for_day(airport: str, station: dict, day: date) -> list[dict]:
    comps = {str(k): (v or {}).get("title", "") for k, v in
             (station.get("companiesById") or {}).items()}
    raw = []
    for t in station.get("threads") or []:
        f = _flight(t, comps)
        if f is None:
            continue
        if _flight_day(f) != day:
            continue
        raw.append(f)
    merged = _merge_codeshares(raw)
    kept = []
    skipped = {}
    for f in merged:
        f["terminal"] = _terminal(airport, f)
        if f["status"] in SKIP_STATUSES:
            skipped["unknown"] = skipped.get("unknown", 0) + 1
            continue
        if f["terminal"] in BUSINESS_TERMINALS.get(airport, ()):
            skipped["business"] = skipped.get("business", 0) + 1
            continue
        kept.append(f)
    if skipped:
        log.info("[%s] %s: не учтено %s", airport, day, skipped)
    merged = kept
    for f in merged:
        f["airlines"] = []
        for c in f["company_ids"]:
            name = comps.get(str(c), "")
            if name and name not in f["airlines"]:
                f["airlines"].append(name)
    merged.sort(key=lambda f: (f["sched"], f["numbers"][0] if f["numbers"] else ""))
    return merged


def csv_rows(airport: str, day: date, flights: list[dict]) -> list[dict]:
    """Строки data/daily: только не отменённые рейсы (как в прежнем сборе)."""
    rows = []
    for f in flights:
        if f["status"] == "cancelled":
            continue
        rows.append({
            "airport": airport,
            "flight_date": day.isoformat(),
            "scheduled_time": f["sched"].strftime("%H:%M"),
            "actual_time": f["actual"].strftime("%H:%M") if f["actual"] else "",
            "terminal": f["terminal"],
            "gate": f["gate"],
            "airlines": ",".join(f["airlines"]),
            "flight_numbers": ",".join(f["numbers"]),
            "destination": f["dest"],
            "destination_iata": f["dest_iata"],
        })
    return rows


def ops_flights(flights: list[dict]) -> list[dict]:
    """Записи для src.ops_report.summarize."""
    out = []
    for f in flights:
        delay = None
        if f["actual"] is not None:
            delay = int((f["actual"] - f["sched"]).total_seconds() // 60)
        out.append({
            "sched": f["sched"].strftime("%H:%M"),
            "dest": f["dest"],
            "dest_iata": f["dest_iata"],
            "terminal": f["terminal"] or "н/д",
            "gate": f["gate"],
            "status": f["status"],
            "delay": delay,
        })
    return out


def norm_number(n: str) -> str:
    """'U6 271' -> 'U6271'; буква-суффикс AeroDataBox ('DP 1435D') отрезается."""
    n = re.sub(r"\s+", "", str(n or "").upper())
    m = re.match(r"^([A-Z0-9]{2}\d+)[A-Z]$", n)
    return m.group(1) if m else n


def page_keys(station: dict) -> tuple[set, set]:
    nums, sd = set(), set()
    for t in station.get("threads") or []:
        for n in [t.get("number")] + [c.get("number") for c in t.get("codeshares") or []]:
            if n:
                nums.add(norm_number(n))
        dt = _dt((t.get("eventDt") or {}).get("datetime"))
        route = (t.get("routeStations") or [{}])[0] or {}
        if dt is not None and route.get("iataCode"):
            sd.add((dt.strftime("%H:%M"), route["iataCode"]))
    return nums, sd


def adb_extra(airport: str, adb_rows: list[dict], neighbor_numbers=()) -> list[dict]:
    """Строки AeroDataBox, которых нет на странице Яндекса за те же сутки.

    Рейс считается известным Яндексу, если любой его номер есть на странице
    (в любом статусе и с любой датой) или в neighbor_numbers, либо если на
    странице есть рейс с тем же плановым временем и тем же аэропортом
    назначения (кодшеринг под другим номером).
    """
    nums, sd = LAST_PAGE.get(airport, (set(), set()))
    # Перегоны между московскими аэропортами после ухода на запасной
    # (SZ 331 и A9 932 «в Москву» из Шереметьева 18 и 20.08) и борты без
    # аэропорта назначения (грузовые) пассажирским вылетом не считаем.
    known = set(nums) | {norm_number(n) for n in neighbor_numbers}
    out = []
    for r in adb_rows:
        rn = {norm_number(n) for n in str(r.get("flight_numbers", "")).split(",") if n.strip()}
        if rn & known:
            continue
        dest = (r.get("destination_iata") or "").strip().upper()
        if not dest or dest in MOSCOW_IATA:
            continue
        if (r.get("scheduled_time"), dest) in sd:
            continue
        out.append(r)
    return out


def ops_from_csv(rows: list[dict]) -> list[dict]:
    """Записи для summarize из строк data/daily (добавленных из AeroDataBox)."""
    out = []
    for r in rows:
        delay = None
        try:
            sh, sm = (int(x) for x in r["scheduled_time"].split(":"))
            ah, am = (int(x) for x in r["actual_time"].split(":"))
            delay = (ah * 60 + am) - (sh * 60 + sm)
            if delay > 720:
                delay -= 1440
            elif delay <= -720:
                delay += 1440
        except (KeyError, ValueError, AttributeError):
            delay = None
        out.append({
            "sched": r.get("scheduled_time", ""),
            "dest": r.get("destination", ""),
            "dest_iata": r.get("destination_iata", ""),
            "terminal": r.get("terminal") or "н/д",
            "gate": r.get("gate") or "",
            "status": "departed",
            "delay": delay,
        })
    return out


def fetch_day(airport: str, day: date,
              client: Optional[httpx.Client] = None) -> list[dict]:
    """Физические рейсы аэропорта за сутки. Бросает YandexError при неудаче."""
    station = fetch_station(airport, day, client)
    LAST_PAGE[airport] = page_keys(station)
    flights = flights_for_day(airport, station, day)
    if len(flights) < MIN_FLIGHTS:
        raise YandexError("[%s] %s: всего %d рейсов, страница неполная"
                          % (airport, day, len(flights)))
    n_c = sum(1 for f in flights if f["status"] == "cancelled")
    n_g = sum(1 for f in flights if f["gate"])
    log.info("[%s] %s: Яндекс %d рейсов (отменено %d, с гейтом %d)",
             airport, day, len(flights), n_c, n_g)
    return flights


if __name__ == "__main__":
    import sys
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=2)
    for ap in AIRPORT_CONFIGS:
        fl = fetch_day(ap, d)
        print(ap, len(fl))
