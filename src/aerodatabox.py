"""Источник данных по всем трём аэропортам — исторический FIDS AeroDataBox.

Новая архитектура (раз в сутки вместо снапшотов каждые 10 минут):
 - история AeroDataBox хранит гейт/терминал/фактическое время и ПОСЛЕ вылета,
   поэтому достаточно одного захода в день за прошедшие сутки;
 - вчерашний день = 2 окна по 12 часов (ограничение API) на аэропорт,
   3 аэропорта = 6 запросов/день ~= 180/мес — влезает даже в лимит 300;
 - снапшоты, дедуп тиков, эвристики «вылетел/не вылетел» и Яндекс больше
   не нужны: статус Departed приходит явно.

Эндпоинт (по датам, локальное время аэропорта):
  GET /flights/airports/{codeType}/{code}/{fromLocal}/{toLocal}
  fromLocal/toLocal в формате YYYY-MM-DDTHH:MM (без таймзоны, оно локальное).
"""
from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from src.config import DATA_DIR, REQUEST_TIMEOUT_SEC
from src.utils import get_logger

log = get_logger("aerodatabox")

API_HOST = "aerodatabox.p.rapidapi.com"

# Аэропорты сбора. Терминал у DME API не отдаёт отдельным полем —
# извлекаем зону из первой буквы гейта (D13 -> D). У SVO/VKO terminal есть.
AIRPORTS = ("SVO", "VKO", "DME")

# Бюджет запросов в месяц (страховка от выхода за лимит RapidAPI).
# 6 запросов/день * ~30 дней ~= 180. Потолок с запасом.
# Если план Basic = 300/мес — 250 безопасно. Если 600 — можно поднять.
MONTHLY_BUDGET = 250
USAGE_FILE = DATA_DIR / "aerodatabox_usage.json"

DEPARTED_STATUSES = {"departed", "enroute", "arrived"}
EXCLUDED_STATUSES = {"canceled", "cancelled", "diverted", "canceleduncertain"}

# Пауза между запросами (секунд) — чтобы не упереться в rate limit «в секунду».
REQUEST_PAUSE_SEC = 3
# Сколько раз повторить запрос при HTTP 429 (rate limit) и пауза перед повтором.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_SEC = 5


# ---------- учёт расхода квоты ----------

def _load_usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _month() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m")


def remaining_budget() -> int:
    return max(0, MONTHLY_BUDGET - _load_usage().get(_month(), 0))


def _bump_usage(n: int = 1) -> int:
    usage = _load_usage()
    m = _month()
    usage[m] = usage.get(m, 0) + n
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(usage, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return usage[m]


# ---------- запрос к API ----------

class AeroDataBoxError(Exception):
    pass


def fetch_window(api_key: str, airport: str, from_local: datetime,
                 to_local: datetime, client: Optional[httpx.Client] = None) -> dict:
    """Запросить вылеты аэропорта за окно [from_local, to_local] (<=12ч)."""
    f = from_local.strftime("%Y-%m-%dT%H:%M")
    t = to_local.strftime("%Y-%m-%dT%H:%M")
    url = f"https://{API_HOST}/flights/airports/iata/{airport}/{f}/{t}"
    params = {
        "direction": "Departure",
        "withLeg": "false",
        "withCancelled": "true",
        "withCodeshared": "true",   # нужны, чтобы собрать все номера рейса
        "withCargo": "false",
        "withPrivate": "false",
        "withLocation": "false",
    }
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": API_HOST,
        "Accept": "application/json",
    }

    def _do_get():
        if client is None:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as c:
                return c.get(url, params=params, headers=headers)
        return client.get(url, params=params, headers=headers)

    last_err = None
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            r = _do_get()
            if r.status_code == 429:
                # rate limit — ждём и повторяем
                wait = RATE_LIMIT_BACKOFF_SEC * attempt
                log.warning("[%s] HTTP 429 (rate limit), попытка %d/%d, ждём %dс",
                            airport, attempt, RATE_LIMIT_RETRIES, wait)
                last_err = AeroDataBoxError("HTTP 429: rate limit")
                _time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise AeroDataBoxError(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, ValueError) as e:
            last_err = AeroDataBoxError(str(e))
            _time.sleep(RATE_LIMIT_BACKOFF_SEC)
    raise last_err or AeroDataBoxError("неизвестная ошибка запроса")


# ---------- разбор и сборка ----------

def _parse_local(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T", 1))
    except ValueError:
        return None


def _clean_field(v) -> Optional[str]:
    """Убрать повторы внутри значения: '20A,20A' -> '20A', '103,103' -> '103'.
    AeroDataBox иногда дублирует токены в gate/checkInDesk."""
    if v is None:
        return None
    parts = [p.strip() for p in str(v).split(",") if p.strip()]
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return ",".join(out) if out else None


def _terminal(dep: dict) -> Optional[str]:
    """Терминал/зона вылета. У SVO/VKO есть поле terminal; у DME его нет —
    берём первую букву гейта (D13 -> D)."""
    t = dep.get("terminal")
    if t:
        return _clean_field(t)
    gate = _clean_field(dep.get("gate"))
    if gate and gate[0].isalpha():
        return gate[0].upper()
    return None


def _is_departed(status: str) -> bool:
    return (status or "").strip().lower() in DEPARTED_STATUSES


def _is_excluded(status: str) -> bool:
    return (status or "").strip().lower() in EXCLUDED_STATUSES


def build_day_rows(airport: str, payloads: list[dict]) -> list[dict]:
    """Собрать чистые строки за день. Группировка по физическому борту:
    (терминал, гейт, плановое_время, направление). Кодшеринги схлопываются:
    все номера в одну ячейку, оператор первым. Только вылетевшие пассажирские.
    """
    groups: dict[tuple, dict] = {}

    for payload in payloads:
        for item in (payload.get("departures") or []):
            if item.get("isCargo"):
                continue
            status = item.get("status") or ""
            if _is_excluded(status) or not _is_departed(status):
                continue

            # При withLeg=false вылетная инфа лежит в "movement";
            # при withLeg=true — в "departure" (+ "arrival" с пунктом назначения).
            dep = item.get("movement") or item.get("departure") or {}
            sched = _parse_local((dep.get("scheduledTime") or {}).get("local"))
            if sched is None:
                continue

            gate = _clean_field(dep.get("gate"))
            terminal = _terminal(dep)
            # Направление: в режиме movement аэропорт назначения лежит прямо в
            # dep["airport"]; в режиме departure/arrival — в item["arrival"].
            arr_airport = (dep.get("airport")
                           or (item.get("arrival") or {}).get("airport")
                           or {})
            dest = arr_airport.get("name") or arr_airport.get("iata") or "?"
            dest_iata = arr_airport.get("iata") or ""

            number = (item.get("number") or "").strip()
            airline = (item.get("airline") or {}).get("name") or ""
            cs = (item.get("codeshareStatus") or "").strip().lower()
            is_operator = cs == "isoperator"
            revised = _parse_local((dep.get("revisedTime") or {}).get("local"))
            runway = _parse_local((dep.get("runwayTime") or {}).get("local"))
            at = revised or runway

            # Ключ склейки БЕЗ гейта: один физический борт = (время вылета,
            # направление). Гейт в ключ не входит, т.к. на границе 12ч-окон
            # он может быть пустым в одном окне и заполненным/другим в другом,
            # что иначе плодит дубли одного рейса. Чтобы РАЗНЫЕ рейсы в одно
            # время в один город (разные перевозчики, не кодшеринг) не слиплись,
            # добавляем в ключ номер рейса-оператора, если он известен.
            op_number = number if is_operator else None
            key = (sched.replace(tzinfo=None).isoformat(),
                   dest_iata or dest, op_number)
            g = groups.get(key)
            if g is None:
                g = {
                    "airport": airport,
                    "flight_date": sched.date().isoformat(),
                    "scheduled_time": sched.strftime("%H:%M"),
                    "actual_time": at.strftime("%H:%M") if at else "",
                    "terminal": terminal or "",
                    "gate": gate or "",
                    "destination": dest,
                    "destination_iata": dest_iata,
                    "operators": [],
                    "_best_at": at,  # для выбора последнего фактического времени
                }
                groups[key] = g
            else:
                # Берём непустой/более свежий гейт и терминал (учёт смены гейта
                # между окнами): заполняем, если было пусто.
                if gate and not g["gate"]:
                    g["gate"] = gate
                if terminal and not g["terminal"]:
                    g["terminal"] = terminal
                # фактическое время — берём максимально позднее известное
                if at and (g["_best_at"] is None or at > g["_best_at"]):
                    g["_best_at"] = at
                    g["actual_time"] = at.strftime("%H:%M")
            g["operators"].append((number, airline, is_operator))

    rows: list[dict] = []
    for g in groups.values():
        g.pop("_best_at", None)
        ops = g.pop("operators")
        ordered = [o for o in ops if o[2]] + [o for o in ops if not o[2]]
        seen, numbers, airlines = set(), [], []
        for num, al, _ in ordered:
            if num and num not in seen:
                seen.add(num)
                numbers.append(num)
                if al and al not in airlines:
                    airlines.append(al)
        g["flight_numbers"] = ",".join(numbers)
        g["airlines"] = ",".join(airlines)
        rows.append(g)

    rows.sort(key=lambda r: (r["scheduled_time"], r["terminal"], r["gate"]))
    return rows


def fetch_airport_day(api_key: str, airport: str, day: date,
                      client: httpx.Client) -> list[dict]:
    """Забрать полный день аэропорта (2 окна по 12ч) и собрать строки."""
    start = datetime(day.year, day.month, day.day, 0, 0)
    windows = [
        (start, start + timedelta(hours=12)),
        (start + timedelta(hours=12), start + timedelta(hours=24)),
    ]
    payloads = []
    for idx, (f, t) in enumerate(windows):
        if remaining_budget() <= 0:
            raise AeroDataBoxError(
                f"месячный бюджет AeroDataBox исчерпан (лимит {MONTHLY_BUDGET})")
        if idx > 0:
            _time.sleep(REQUEST_PAUSE_SEC)  # не упираться в rate limit «в секунду»
        payloads.append(fetch_window(api_key, airport, f, t, client=client))
        _bump_usage()
    rows = build_day_rows(airport, payloads)
    log.info("[%s] %s: собрано %d вылетевших рейсов", airport, day, len(rows))
    return rows
