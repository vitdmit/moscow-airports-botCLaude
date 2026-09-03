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
#
# ИСПРАВЛЕНО 03.09.2026. Раньше здесь стояло 650 с пометкой «тариф Basic,
# 700 запросов». Тариф на самом деле Pro за 5.35 долларов в месяц, и там два
# счётчика: 24 000 запросов и 6 000 API Units в месяц, оба с жёстким лимитом.
# Упираемся мы в Units: эндпоинт FIDS это Tier 2, то есть 2 Units за запрос.
# Значит потолок по нашим запросам 6000 / 2 = 3000 в месяц, а не 700.
# Сверка: на 03.09.2026 кабинет показывал 16.33% квоты при 502 запросах
# по нашему счётчику, что даёт 980 Units и ровно 2 Units на запрос.
#
# Ставим 2800: запас 200 запросов до жёсткого лимита. Перерасход оплатить
# нельзя: обе квоты помечены Hard Limit, запросы сверх них просто отбиваются.
# Штатный расход с ночным окном: 9 в сутки, около 270 в месяц, то есть
# десятая часть лимита. Счётчик по календарному месяцу, обнуляется 1-го числа.
MONTHLY_BUDGET = 2800
USAGE_FILE = DATA_DIR / "aerodatabox_usage.json"

DEPARTED_STATUSES = {"departed", "enroute", "arrived"}
EXCLUDED_STATUSES = {"canceled", "cancelled", "diverted", "canceleduncertain"}

# Сырые ответы последнего сбора по каждому аэропорту. Нужны src/ops_report.py,
# чтобы посчитать план, отмены и задержки без повторных запросов к API.
LAST_PAYLOADS: dict = {}

# Пауза между запросами (секунд) — чтобы не упереться в rate limit «в секунду».
REQUEST_PAUSE_SEC = 3
# Сколько раз повторить запрос при HTTP 429 (rate limit) и пауза перед повтором.
RATE_LIMIT_RETRIES = 6
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


LATE_HOUR = 18  # с этого часа рейс считаем «поздним», он может уйти за полночь


def _belongs_to_day(item: dict, window_idx: int, has_tail: bool) -> bool:
    """Относится ли рейс из окна window_idx к собираемым суткам.

    API отбирает рейсы в окно по ФАКТИЧЕСКОМУ времени и вдобавок иногда
    подставляет в scheduledTime дату окна запроса, а не реальную. Поэтому дате
    в ответе не доверяем и разделяем сутки по времени суток:

      окно 1 (00:00-12:00): поздний вечер здесь это ВЧЕРАШНИЙ рейс, ушедший
        после полуночи. Такие выбрасываем, иначе в сутках оказывается два
        одинаковых рейса: свой и вчерашний (за июнь-сентябрь так задвоилось
        72 строки, все между 22:00 и 02:00).
      окно 2 (12:00-24:00): всё своё.
      окно 3 ((day+1) 00:00-06:00): наоборот, своим считаем только поздний
        вечер, это наши рейсы, уехавшие за полночь. Утренние рейсы следующих
        суток отбрасываем.
    """
    dep = item.get("movement") or item.get("departure") or {}
    sched = _parse_local((dep.get("scheduledTime") or {}).get("local"))
    if sched is None:
        return False
    late = sched.hour >= LATE_HOUR
    if window_idx == 0:
        return not late
    if has_tail and window_idx == 2:
        return late
    return True


def _filter_payloads_by_window(pairs: list, has_tail: bool):
    """pairs это [(номер окна, ответ)]. Отдаёт (очищенные ответы, сколько
    рейсов дало ночное окно).

    Номер окна храним явно. Любое окно может ответить 204 и выпасть из
    списка, тогда позиция в списке перестаёт совпадать с номером окна,
    а правило разделения суток завязано именно на номер.
    """
    out, tail = [], 0
    for idx, payload in pairs:
        deps = [it for it in (payload.get("departures") or [])
                if _belongs_to_day(it, idx, has_tail)]
        p = dict(payload)
        p["departures"] = deps
        out.append(p)
        if has_tail and idx == 2:
            tail = len(deps)
    return out, tail


def _tail_window_affordable(airports: int = 3, per_day: int = 2) -> bool:
    """Хватит ли бюджета на третье окно (ночной хвост) без риска для месяца.

    Штатный сбор до конца месяца стоит airports * per_day запросов в сутки.
    Третье окно берём только если после резерва на эти сутки ещё что-то есть.
    """
    import calendar
    today = date.today()
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    reserve = days_left * airports * per_day
    return remaining_budget() - reserve >= airports


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


class NoDataYetError(AeroDataBoxError):
    """Данные за запрошенный период ещё не наполнены в истории API (HTTP 204).
    Повторять в рамках того же запуска бесполезно — нужно дособрать позже."""
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
                wait = RATE_LIMIT_BACKOFF_SEC * attempt
                log.warning("[%s] HTTP 429 (rate limit), попытка %d/%d, ждём %dс",
                            airport, attempt, RATE_LIMIT_RETRIES, wait)
                last_err = AeroDataBoxError("HTTP 429: rate limit")
                _time.sleep(wait)
                continue
            # Пишем в лог остаток квоты со стороны RapidAPI. Наш внутренний
            # счётчик это оценка, а здесь настоящий лимит тарифа. Нужно, чтобы
            # решить, когда включать ночное окно (плюс 90 запросов в месяц).
            _left = r.headers.get("x-ratelimit-requests-remaining")
            _lim = r.headers.get("x-ratelimit-requests-limit")
            if _left is not None:
                log.info("квота RapidAPI: осталось %s из %s", _left, _lim)
            r.raise_for_status()
            if r.status_code == 204:
                # 204 No Content — данных за этот период ещё нет в истории API
                # (наполнение FIDS по аэропорту отстаёт). Повторять бесполезно:
                # сразу сообщаем особой ошибкой, чтобы день дособрать позже.
                raise NoDataYetError(
                    f"[{airport}] HTTP 204: данные за период ещё не наполнены")
            body = r.text or ""
            if not body.strip():
                # пустое тело при 200 — возможно временный сбой, пара повторов
                wait = RATE_LIMIT_BACKOFF_SEC * attempt
                log.warning("[%s] пустой ответ (статус %s), попытка %d/%d, ждём %dс",
                            airport, r.status_code, attempt, RATE_LIMIT_RETRIES, wait)
                last_err = AeroDataBoxError("пустой ответ от API")
                _time.sleep(wait)
                continue
            try:
                return r.json()
            except ValueError:
                # тело есть, но не JSON (HTML-страница ошибки и т.п.) — повтор
                wait = RATE_LIMIT_BACKOFF_SEC * attempt
                log.warning("[%s] не-JSON ответ (статус %s), попытка %d/%d, ждём %dс",
                            airport, r.status_code, attempt, RATE_LIMIT_RETRIES, wait)
                last_err = AeroDataBoxError("не-JSON ответ от API")
                _time.sleep(wait)
                continue
        except httpx.HTTPStatusError as e:
            raise AeroDataBoxError(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            last_err = AeroDataBoxError(str(e))
            _time.sleep(RATE_LIMIT_BACKOFF_SEC * attempt)
    raise last_err or AeroDataBoxError("неизвестная ошибка запроса")


# ---------- разбор и сборка ----------

def _parse_local(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T", 1))
    except ValueError:
        return None


def _norm_dest(name: str, iata: str) -> str:
    """Канонический ключ направления для склейки. Используем нормализованное
    ИМЯ (не IATA): в кодшеринге у партнёров IATA может быть только у одного
    (напр. YC 90 -> NUX, UT 4090 -> пусто), и по IATA они бы не сошлись.
    Нормализуем имя: нижний регистр, только буквы, схлопнуть транслит-варианты
    (Novyy Urengoy / Novy Urengoj -> одно)."""
    s = (name or iata or "").strip().lower()
    s = "".join(ch for ch in s if ch.isalpha())
    s = s.replace("yy", "y").replace("oj", "oy").replace("ij", "iy")
    return s


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


def build_day_rows(airport: str, payloads: list[dict],
                   target_day: Optional[date] = None) -> list[dict]:
    """Собрать чистые строки за день. Группировка по физическому борту.
    Кодшеринги схлопываются. Только вылетевшие пассажирские.

    Если задан target_day, плановая дата рейса принудительно берётся как
    target_day (историч. FIDS привязывает дату к окну запроса, а не к реальным
    суткам — поэтому доверяем времени суток, а день берём целевой). Факт
    корректируется относительно плана: переход через полночь даёт +1 день.
    """
    groups: dict[tuple, list[dict]] = {}

    for payload in payloads:
        for item in (payload.get("departures") or []):
            if item.get("isCargo"):
                continue
            status = item.get("status") or ""
            if _is_excluded(status):  # 02.09.2026: режем только отменённые и диверсии
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
            is_codeshared = cs == "iscodeshared"
            revised = _parse_local((dep.get("revisedTime") or {}).get("local"))
            runway = _parse_local((dep.get("runwayTime") or {}).get("local"))
            at = revised or runway

            # Нормализация дат. Историч. FIDS привязывает абсолютную дату к окну
            # запроса, а не к реальным суткам рейса. Поэтому, если знаем целевой
            # день, берём плановую дату = target_day (доверяем времени суток),
            # а фактическую дату вычисляем относительно плана по разнице ВРЕМЕНИ
            # суток (переход через полночь -> соседние сутки).
            if at is not None and sched is not None:
                # разница по времени суток в минутах, приведённая к (-720..+720]
                smin = sched.hour * 60 + sched.minute
                amin = at.hour * 60 + at.minute
                delta = amin - smin
                if delta > 720:
                    delta -= 1440
                elif delta <= -720:
                    delta += 1440
                # delta теперь реальное отклонение факта от плана в минутах
                fact_offset_min = delta
            else:
                fact_offset_min = 0

            sched_naive = sched.replace(tzinfo=None)
            # Плановая дата: если знаем целевой день — принудительно он
            # (FIDS привязывает дату к окну запроса, не к реальным суткам).
            if target_day is not None:
                plan_date = target_day
            else:
                plan_date = sched_naive.date()
            plan_dt = datetime(plan_date.year, plan_date.month, plan_date.day,
                               sched_naive.hour, sched_naive.minute)
            # Фактическая дата = плановая + смещение факта (через полночь).
            if at is not None:
                fact_dt = plan_dt + timedelta(minutes=fact_offset_min)
                actual_time = at.strftime("%H:%M")
                actual_date = fact_dt.date()
            else:
                actual_time = ""
                actual_date = plan_date

            # ключ группировки — плановая ДАТА + время суток + направление.
            # Дата в ключе обязательна: иначе рейсы разных суток с одинаковым
            # временем суток (ежедневный рейс) слипнутся в один.
            key = (plan_date.isoformat(), sched_naive.strftime("%H:%M"),
                   _norm_dest(dest, dest_iata))
            groups.setdefault(key, []).append({
                "airport": airport,
                "flight_date": plan_date.isoformat(),
                "actual_date": actual_date.isoformat(),
                "scheduled_time": sched_naive.strftime("%H:%M"),
                "sched_dt": plan_dt,
                "at_time": actual_time,
                "fact_dt": (plan_dt + timedelta(minutes=fact_offset_min)) if at is not None else None,
                "terminal": terminal or "",
                "gate": gate or "",
                "destination": dest,
                "destination_iata": dest_iata,
                "number": number,
                "airline": airline,
                "is_operator": is_operator,
                "is_codeshared": is_codeshared,
            })

    rows: list[dict] = []
    for obs_list in groups.values():
        rows.extend(_resolve_group(obs_list))

    rows = _merge_near_time_dupes(rows)
    rows.sort(key=lambda r: (r["scheduled_time"], r["terminal"], r["gate"]))
    return rows


def _merge_near_time_dupes(rows: list[dict]) -> list[dict]:
    """Слить строки одного борта, попавшие в разные 12ч-окна с чуть разным
    плановым временем (напр. 22:25 и 22:30). Признак одного борта: совпадают
    аэропорт, направление и номер рейса-оператора (первый в flight_numbers),
    а плановое время отличается не более чем на 20 минут."""
    def to_min(hhmm: str) -> int:
        try:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return -10 ** 6

    def op_num(r: dict) -> str:
        return (r["flight_numbers"].split(",")[0] if r["flight_numbers"] else "")

    used = [False] * len(rows)
    out: list[dict] = []
    for i, r in enumerate(rows):
        if used[i]:
            continue
        group = [r]
        for j in range(i + 1, len(rows)):
            if used[j]:
                continue
            o = rows[j]
            if (o["airport"] == r["airport"]
                    and o["flight_date"] == r["flight_date"]
                    and o["destination_iata"] == r["destination_iata"]
                    and op_num(o) and op_num(o) == op_num(r)
                    and abs(to_min(o["scheduled_time"]) - to_min(r["scheduled_time"])) <= 20):
                group.append(o)
                used[j] = True
        used[i] = True
        if len(group) == 1:
            out.append(r)
        else:
            out.append(_merge_rows(group))
    return out


def _merge_rows(group: list[dict]) -> dict:
    """Слить уже готовые строки одного борта в одну (берём раннее плановое
    время, непустой гейт/терминал, позднее фактическое, объединённые номера)."""
    group_sorted = sorted(group, key=lambda r: r["scheduled_time"])
    base = dict(group_sorted[0])
    for r in group_sorted[1:]:
        if not base["gate"] and r["gate"]:
            base["gate"] = r["gate"]
        if not base["terminal"] and r["terminal"]:
            base["terminal"] = r["terminal"]
        if r["actual_time"] > base["actual_time"]:
            base["actual_time"] = r["actual_time"]
        # объединяем номера/авиакомпании без дублей
        nums = base["flight_numbers"].split(",") if base["flight_numbers"] else []
        for n in (r["flight_numbers"].split(",") if r["flight_numbers"] else []):
            if n and n not in nums:
                nums.append(n)
        base["flight_numbers"] = ",".join(nums)
        als = base["airlines"].split(",") if base["airlines"] else []
        for a in (r["airlines"].split(",") if r["airlines"] else []):
            if a and a not in als:
                als.append(a)
        base["airlines"] = ",".join(als)
    return base


def _resolve_group(obs: list[dict]) -> list[dict]:
    """Из наблюдений с одинаковыми (время, направление) собрать строки.

    Один физический борт = один гейт. Разные непустые гейты у НЕ-кодшеринговых
    рейсов => это разные борта (например Аэрофлот и Победа в один город в одно
    время) — разделяем по гейту. Кодшеринг (один борт, много номеров) и дубли
    на границе 12ч-окон (тот же номер, гейт пустой/сменился) — сливаются."""
    # непустые гейты среди "настоящих" рейсов (оператор/обычный, не кодшеринг)
    real_gates = {o["gate"] for o in obs
                  if o["gate"] and not o["is_codeshared"]}

    # Если все НЕ-кодшеринговые наблюдения относятся к одному номеру рейса —
    # это один борт, даже если гейт между окнами переназначили (104 -> 120).
    op_numbers = {o["number"] for o in obs
                  if o["number"] and not o["is_codeshared"]}

    if len(real_gates) <= 1 or len(op_numbers) <= 1:
        # один борт (или гейт неизвестен, или единый номер) — одна строка
        return [_merge_obs(obs)]

    # несколько разных гейтов => группируем по гейту (разные борта).
    # Кодшеринг-наблюдения без своего гейта прицепляем к borту с тем же номером,
    # иначе — к первой группе.
    by_gate: dict[str, list[dict]] = {}
    leftovers: list[dict] = []
    for o in obs:
        if o["gate"]:
            by_gate.setdefault(o["gate"], []).append(o)
        else:
            leftovers.append(o)
    # раскидать безгейтовые наблюдения по номеру рейса, если номер встречается
    num_to_gate = {}
    for gate, lst in by_gate.items():
        for o in lst:
            if o["number"]:
                num_to_gate[o["number"]] = gate
    for o in leftovers:
        g = num_to_gate.get(o["number"])
        if g:
            by_gate[g].append(o)
        else:
            # некуда отнести — отдельная строка
            by_gate.setdefault(o["gate"] or f"_{o['number']}", []).append(o)
    return [_merge_obs(lst) for lst in by_gate.values()]


def _merge_obs(obs: list[dict]) -> dict:
    """Слить список наблюдений одного борта в одну строку."""
    base = obs[0]
    out = {
        "airport": base["airport"],
        "flight_date": base["flight_date"],
        "scheduled_time": base["scheduled_time"],
        "actual_time": "",
        "terminal": "",
        "gate": "",
        "destination": base["destination"],
        "destination_iata": base["destination_iata"],
    }
    # гейт/терминал — первый непустой
    for o in obs:
        if not out["gate"] and o["gate"]:
            out["gate"] = o["gate"]
        if not out["terminal"] and o["terminal"]:
            out["terminal"] = o["terminal"]
    # фактическое время — максимально позднее известное;
    # дата рейса (правило Б) = дата фактического вылета (fact_dt).
    best_fact = None
    for o in obs:
        if o.get("fact_dt") and (best_fact is None or o["fact_dt"] > best_fact):
            best_fact = o["fact_dt"]
    if best_fact:
        out["actual_time"] = best_fact.strftime("%H:%M")
        out["flight_date"] = best_fact.date().isoformat()
    else:
        out["flight_date"] = base["flight_date"]
    # номера: оператор первым, затем остальные; без дублей
    ordered = [o for o in obs if o["is_operator"]] + \
              [o for o in obs if not o["is_operator"]]
    seen, numbers, airlines = set(), [], []
    for o in ordered:
        num = o["number"]
        if num and num not in seen:
            seen.add(num)
            numbers.append(num)
            if o["airline"] and o["airline"] not in airlines:
                airlines.append(o["airline"])
    out["flight_numbers"] = ",".join(numbers)
    out["airlines"] = ",".join(airlines)
    return out


def fetch_airport_day(api_key: str, airport: str, day: date,
                      client: httpx.Client) -> list[dict]:
    """Собрать рейсы, ФАКТИЧЕСКИ вылетевшие в сутки `day` (правило Б).

    Рейс относится к дате по фактическому вылету. Поэтому запрашиваем по
    плановому времени с запасом ±3 часа на стыках суток (поздний вечер
    предыдущего дня может фактически уйти после полуночи `day`, а поздний
    вечер `day` — уйти уже в следующие сутки), затем фильтруем строки по
    flight_date == day. Так ничего не теряется и не задваивается.

    Окна (по плановому локальному времени), все начинаются в «ровные» часы:
      1) day 00:00 .. day 12:00
      2) day 12:00 .. day 24:00
      3) (day+1) 00:00 .. (day+1) 06:00  — утренний хвост СЛЕДУЮЩЕГО дня.
         Запрашивается ТОЛЬКО если этот следующий день уже полностью в прошлом.
         Иначе (если day+1 — сегодня или будущее) данных в истории ещё нет и
         API отвечает HTTP 204. Хвостовые ночные рейсы при пропуске окна 3 не
         теряются: они соберутся при штатном сборе следующего дня (его окна
         1-2 покроют их по фактической дате — правило Б).
    Затем оставляем строки с фактической датой == day.
    """
    d0 = datetime(day.year, day.month, day.day, 0, 0)
    windows = [
        (d0, d0 + timedelta(hours=12)),
        (d0 + timedelta(hours=12), d0 + timedelta(hours=24)),
    ]
    # ИЗМЕНЕНИЕ 2026-07: окно 3 ((day+1) 00:00-06:00) убрано. Его строки имеют
    # плановую дату day+1 и ПОЛНОСТЬЮ отбрасывались фильтром ниже
    # (kept = flight_date == day) — то есть 3 запроса/день (~90/мес, треть
    # штатного расхода) тратились впустую. День рейса определяется по
    # ПЛАНОВОЙ дате — так же, как в ручном учёте коллег, поэтому окон 1-2
    # достаточно: они покрывают все плановые времена суток day.
    # ВОЗВРАТ ОКНА 3, 03.09.2026. Комментарий выше был неверен. Диагностика
    # (src/diag_times.py, data/diag/times_2026-09-01.csv) показала, что API
    # отбирает рейсы в окно по ФАКТИЧЕСКОМУ времени, а не по плановому: в
    # окнах 01.09 пришло 7 рейсов Шереметьева и 9 Внукова с плановой датой
    # 31.08, они ушли после полуночи. Значит наши собственные ночные хвосты
    # лежат в окне следующих суток, а мы его не запрашивали и теряли их:
    # около 8 рейсов в сутки по SVO и 9 по VKO, это 1.8% и 5.5% месяца.
    # За 31.08 из 16 таких рейсов 14 ушли до 01:00, крайний в 02:06,
    # поэтому окна до 06:00 хватает с запасом.
    #
    # Окно 3 стоит один запрос на аэропорт в сутки, 90 в месяц. Берём его,
    # только если после этого останется чем добрать оставшиеся дни месяца
    # штатными двумя окнами. В сентябре 2026 запас съеден пересборами,
    # поэтому окно 3 само включится с 1 октября.
    if day + timedelta(days=1) < date.today() and _tail_window_affordable():
        nd = d0 + timedelta(days=1)
        windows.append((nd, nd + timedelta(hours=6)))

    payloads = []
    empty_windows = 0
    for idx, (f, t) in enumerate(windows):
        if remaining_budget() <= 0:
            raise AeroDataBoxError(
                f"месячный бюджет AeroDataBox исчерпан (лимит {MONTHLY_BUDGET})")
        if idx > 0:
            _time.sleep(REQUEST_PAUSE_SEC)  # не упираться в rate limit «в секунду»
        try:
            payloads.append((idx, fetch_window(api_key, airport, f, t, client=client)))
        except NoDataYetError:
            # 204 в ОТДЕЛЬНОМ окне = в этом окне рейсов/данных нет. Это не повод
            # ронять весь день: окна 1-2 могут содержать данные. Пропускаем окно.
            empty_windows += 1
            log.info("[%s] %s: окно %s..%s пусто (204) — пропускаю",
                     airport, day, f.strftime("%d %H:%M"), t.strftime("%d %H:%M"))
        _bump_usage()
    # день считаем «нет данных», только если ВСЕ окна пустые
    if empty_windows == len(windows):
        raise NoDataYetError(
            f"[{airport}] HTTP 204: все окна за {day} пусты — данных нет")
    has_tail = len(windows) > 2
    raw_total = sum(len(p.get("departures") or []) for _, p in payloads)
    payloads, tail = _filter_payloads_by_window(payloads, has_tail)
    clean_total = sum(len(p.get("departures") or []) for p in payloads)
    log.info("[%s] %s: в ответе %d записей, после разделения суток осталось %d",
             airport, day, raw_total, clean_total)
    LAST_PAYLOADS[airport] = payloads
    # Плановую дату принудительно ставим day: чужие сутки уже отфильтрованы
    # по окнам, а дата в ответе API ненадёжна.
    rows = build_day_rows(airport, payloads, target_day=day)
    target = day.isoformat()
    kept = [r for r in rows if r["flight_date"] == target]
    dropped = len(rows) - len(kept)
    log.info("[%s] %s: собрано %d (отброшено %d вне суток по факту)",
             airport, day, len(kept), dropped)
    if has_tail:
        log.info("[%s] %s: ночное окно добавило %d рейсов, уехавших за полночь",
                 airport, day, tail)
    return kept
