# app/config.py
import os
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./zabdata.db")
IS_DEV = os.getenv("ENV", "development") == "development"
JWT_SECRET = os.getenv("JWT_SECRET", "PLACEHOLDER")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL", "30"))
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL", "43200"))

if IS_DEV or JWT_SECRET == "PLACEHOLDER":
    logger.warning("\n\nВНИМАНИЕ: JWT_SECRET это пустышка! Нужно его заменить! Или же вы находитесь в режиме разработчика!\n\n")