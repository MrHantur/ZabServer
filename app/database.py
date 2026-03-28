# app/database.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from .config import DATABASE_URL, IS_DEV

engine = create_async_engine(DATABASE_URL, echo=IS_DEV)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""
    pass