"""Константы проекта."""
from pathlib import Path
from zoneinfo import ZoneInfo

# Часовой пояс рейсов — Москва.
MSK = ZoneInfo("Europe/Moscow")

# Идентификаторы станций в Яндекс.Расписаниях.
# Найдены вручную: rasp.yandex.ru/station/{id}/
AIRPORTS: dict[str, dict[str, str]] = {
    "SVO": {"station_id": "9600213", "name": "Шереметьево"},
    "VKO": {"station_id": "9600215", "name": "Внуково"},
    "DME": {"station_id": "9600216", "name": "Домодедово"},
}

# URL табло вылета на Яндекс.Расписаниях.
YANDEX_RASP_URL = (
    "https://rasp.yandex.ru/station/{station_id}/?type=tablo&event=departure"
)

# Сетевые параметры.
REQUEST_TIMEOUT_SEC = 30
# Реалистичный браузерный User-Agent. Честный «ботовый» UA Яндекс
# распознаёт и начинает отдавать заглушки без таблицы (срабатывает
# антибот). Поэтому представляемся обычным Chrome.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
# Полный набор заголовков обычного браузера.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Пути к данным.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
DAILY_DIR = DATA_DIR / "daily"
