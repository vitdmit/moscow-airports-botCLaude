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
# Простой и честный UA. Без маскировки под Chrome — Яндекс отвечает нормально,
# а маскировка через год превращается в неподдерживаемый легаси.
USER_AGENT = (
    "MoscowAirportsBot/1.0 "
    "(personal analytics; respectful crawler; +https://github.com)"
)

# Пути к данным.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
DAILY_DIR = DATA_DIR / "daily"
