"""
Zab API — бэкенд для школьного портала.
Версия 1.3.0

Основные возможности:
  - JWT-аутентификация (access + refresh токены)
  - Роли: viewer, editor, admin, contributor
  - CRUD для олимпиад и расписания (требует авторизации)
  - Публичные эндпоинты расписания и олимпиад (без авторизации)
  - Система предложений изменений для роли contributor
  - Поля имя/фамилия у пользователя
  - Карма пользователя (+1 за одобренное предложение, -1 за отклонённое)

Запуск:
  python server.py                     # dev-режим (auto-reload)
  uvicorn server:app --port 1717       # prod-режим

Переменные окружения:
  DATABASE_URL      — строка подключения к БД (default: sqlite)
  ENV               — "development" или "production"
  JWT_SECRET        — секретный ключ подписи токенов (обязательно задать в prod!)
  ACCESS_TOKEN_TTL  — время жизни access-токена, мин (default: 30)
  REFRESH_TOKEN_TTL — время жизни refresh-токена, мин (default: 43200 = 30 дней)
"""

import json as _json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import AsyncGenerator, Optional

import jwt
from bcrypt import checkpw, gensalt, hashpw
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

DATABASE_URL      = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./zabdata.db")
IS_DEV            = os.getenv("ENV", "development") == "development"
JWT_SECRET        = os.getenv("JWT_SECRET", "PLACEHOLDER")
JWT_ALGORITHM     = "HS256"
ACCESS_TOKEN_TTL  = int(os.getenv("ACCESS_TOKEN_TTL",  "30"))
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL", "43200"))

engine       = create_async_engine(DATABASE_URL, echo=IS_DEV)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Утилиты паролей
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return hashpw(password.encode("utf-8"), gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# OAuth2-схема: авто-ошибка отключена для поддержки необязательной авторизации
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# ORM-модели (SQLAlchemy)
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""
    pass


class UserORM(Base):
    """
    Пользователь системы.

    Роли:
      viewer      — только чтение (авторизованный)
      contributor — может предлагать изменения через /proposals
      editor      — может редактировать данные напрямую + рецензировать proposals
      admin       — полный доступ, включая создание пользователей и удаление записей

    Карма: +1 при одобрении предложения, -1 при отклонении.
    """
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String,  unique=True, nullable=False, index=True)
    password_hash = Column(String,  nullable=False)
    role          = Column(String,  nullable=False, default="viewer")
    first_name    = Column(String,  nullable=True)
    last_name     = Column(String,  nullable=True)
    karma         = Column(Integer, nullable=False, default=0)


class OlympiadORM(Base):
    """Олимпиада."""
    __tablename__ = "olympiads"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String,  nullable=False)
    description = Column(String,  nullable=True)
    subject     = Column(String,  nullable=False)
    date_start  = Column(String,  nullable=False)   # YYYY-MM-DD
    date_end    = Column(String,  nullable=True)    # YYYY-MM-DD
    time        = Column(String,  nullable=True)
    classes     = Column(String,  nullable=False)   # например "9-11"
    stage       = Column(String,  nullable=True)
    level       = Column(Integer, nullable=True)
    link        = Column(String,  nullable=True)


class ScheduleORM(Base):
    """Урок в расписании."""
    __tablename__ = "schedule"

    id          = Column(Integer, primary_key=True, index=True)
    class_name  = Column(String,  nullable=False, index=True)  # например "10A"
    weekday     = Column(Integer, nullable=False, index=True)  # 0=пн … 6=вс
    lesson_num  = Column(Integer, nullable=False)
    subject     = Column(String,  nullable=False)
    teacher     = Column(String,  nullable=True)
    room        = Column(String,  nullable=True)
    time_start  = Column(String,  nullable=True)   # HH:MM
    time_end    = Column(String,  nullable=True)   # HH:MM


class ProposalORM(Base):
    """
    Предложение изменения от contributor.
    Жизненный цикл: pending → approved | rejected.
    """
    __tablename__ = "proposals"

    id          = Column(Integer, primary_key=True, index=True)
    author      = Column(String,  nullable=False, index=True)
    entity_type = Column(String,  nullable=False)                # "olympiad" | "schedule"
    entity_id   = Column(Integer, nullable=True)                 # NULL → create
    action      = Column(String,  nullable=False)                # "create" | "update" | "delete"
    payload     = Column(String,  nullable=False)                # JSON-строка
    status      = Column(String,  nullable=False, default="pending")
    created_at  = Column(String,  nullable=False)                # ISO datetime UTC
    reviewed_by = Column(String,  nullable=True)
    review_note = Column(String,  nullable=True)


# ---------------------------------------------------------------------------
# Pydantic-схемы (DTO)
# ---------------------------------------------------------------------------

# Переиспользуемые ограничения полей
_NAME   = Field(..., min_length=1, max_length=200)
_SHORT  = Field(..., min_length=1, max_length=100)
_OPT30  = Field(None, max_length=30)
_OPT100 = Field(None, max_length=100)
_OPT500 = Field(None, max_length=500)
_TIME   = Field(None, pattern=r"^\d{2}:\d{2}$")


# --- Аутентификация и пользователи ---

class TokenPair(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserCreate(BaseModel):
    username:   str           = Field(..., min_length=3, max_length=50)
    password:   str           = Field(..., min_length=8, max_length=128)
    role:       str           = Field("viewer", pattern=r"^(viewer|contributor|editor|admin)$")
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name:  Optional[str] = Field(None, min_length=1, max_length=100)


class UserRead(BaseModel):
    id:         int
    username:   str
    role:       str
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    karma:      int = 0
    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    """Обновление профиля: только имя и фамилия."""
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name:  Optional[str] = Field(None, min_length=1, max_length=100)


# --- Олимпиады ---

class OlympiadBase(BaseModel):
    name:        str           = _NAME
    description: Optional[str] = _OPT500
    subject:     str           = _SHORT
    date_start:  str           = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_end:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time:        Optional[str] = _TIME
    classes:     str           = Field(..., pattern=r"^(?:[1-9]|10|11)-(?:[1-9]|10|11)$")
    stage:       Optional[str] = _OPT30
    level:       Optional[int] = Field(default=1, ge=1, le=3)
    link:        Optional[str] = _OPT500
    @model_validator(mode="after")
    def check_dates(self):
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end не может быть раньше date_start")
        return self


class OlympiadRead(OlympiadBase):
    id: int
    model_config = {"from_attributes": True}


class OlympiadUpdate(BaseModel):
    """Частичное обновление олимпиады (PATCH)."""
    name:        Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    subject:     Optional[str] = Field(None, min_length=1, max_length=100)
    date_start:  Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_end:    Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time:        Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    classes:     Optional[str] = Field(None, min_length=1, max_length=100)
    level:       Optional[int] = Field(None, ge=1, le=3)
    link:        Optional[str] = Field(None, max_length=500)
    @model_validator(mode="after")
    def check_dates(self):
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end не может быть раньше date_start")
        return self


class OlympiadResponse(BaseModel):
    success: bool
    data:    list[OlympiadRead]
    error:   Optional[str] = None


# --- Расписание ---

class ScheduleBase(BaseModel):
    class_name:  str           = Field(..., min_length=1, max_length=30)
    weekday:     int           = Field(..., ge=0, le=6)
    lesson_num:  int           = Field(..., ge=1, le=20)
    subject:     str           = _SHORT
    teacher:     Optional[str] = _OPT100
    room:        Optional[str] = _OPT30
    time_start:  Optional[str] = _TIME
    time_end:    Optional[str] = _TIME


class ScheduleRead(ScheduleBase):
    id: int
    model_config = {"from_attributes": True}


class ScheduleUpdate(BaseModel):
    """Частичное обновление урока (PATCH)."""
    class_name:  Optional[str] = Field(None, min_length=1, max_length=30)
    weekday:     Optional[int] = Field(None, ge=0, le=6)
    lesson_num:  Optional[int] = Field(None, ge=1, le=20)
    subject:     Optional[str] = Field(None, min_length=1, max_length=100)
    teacher:     Optional[str] = Field(None, max_length=100)
    room:        Optional[str] = Field(None, max_length=30)
    time_start:  Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    time_end:    Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class ScheduleResponse(BaseModel):
    success: bool
    data:    list[ScheduleRead]
    error:   Optional[str] = None


# --- Предложения изменений ---

class ProposalCreate(BaseModel):
    """
    Правила согласованности action/entity_id:
      - create → entity_id=None, payload — полные данные новой записи
      - update → entity_id обязателен, payload — изменяемые поля
      - delete → entity_id обязателен, payload может быть {}
    """
    entity_type: str           = Field(..., pattern=r"^(olympiad|schedule)$")
    entity_id:   Optional[int] = None
    action:      str           = Field(..., pattern=r"^(create|update|delete)$")
    payload:     dict


class ProposalReview(BaseModel):
    decision:    str           = Field(..., pattern=r"^(approved|rejected)$")
    review_note: Optional[str] = Field(None, max_length=500)


class ProposalRead(BaseModel):
    id:          int
    author:      str
    entity_type: str
    entity_id:   Optional[int]
    action:      str
    payload:     dict
    status:      str
    created_at:  str
    reviewed_by: Optional[str] = None
    review_note: Optional[str] = None

    @classmethod
    def from_orm(cls, row: ProposalORM) -> "ProposalRead":
        return cls(
            id=row.id,
            author=row.author,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            action=row.action,
            payload=_json.loads(row.payload),
            status=row.status,
            created_at=row.created_at,
            reviewed_by=row.reviewed_by,
            review_note=row.review_note,
        )


class ProposalResponse(BaseModel):
    success: bool
    data:    list[ProposalRead]
    error:   Optional[str] = None


# ---------------------------------------------------------------------------
# Инициализация FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Создаёт все таблицы при старте приложения."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _token_key(request: Request) -> str:
    """
    Ключ для rate-limiter: username из токена или IP-адрес клиента.
    Срок действия токена при этом не проверяется.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(
                auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False},
            )
            return f"user:{payload.get('sub', 'anon')}"
        except Exception:
            pass
    return f"ip:{request.client.host}"


limiter = Limiter(key_func=_token_key)

app = FastAPI(title="Zab API", version="1.3.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """DI: выдаёт сессию БД на время одного запроса."""
    async with SessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# JWT-утилиты
# ---------------------------------------------------------------------------

def _make_token(sub: str, role: str, ttl_minutes: int, kind: str) -> str:
    """Создаёт подписанный JWT с полями sub, role, kind, iat, exp."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub":  sub,
            "role": role,
            "kind": kind,
            "iat":  now,
            "exp":  now + timedelta(minutes=ttl_minutes),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def _make_token_pair(username: str, role: str) -> TokenPair:
    """Создаёт пару access + refresh токенов."""
    return TokenPair(
        access_token=_make_token(username, role, ACCESS_TOKEN_TTL,  "access"),
        refresh_token=_make_token(username, role, REFRESH_TOKEN_TTL, "refresh"),
    )


def _decode(token: str, expected_kind: str) -> dict:
    """
    Декодирует и валидирует JWT.
    Проверяет подпись, срок действия и тип токена.
    Выбрасывает HTTPException 401 при любой ошибке.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Токен истёк")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Неверный токен")

    if payload.get("kind") != expected_kind:
        raise HTTPException(status_code=401, detail="Неверный тип токена")

    return payload


# ---------------------------------------------------------------------------
# Зависимости аутентификации и авторизации
# ---------------------------------------------------------------------------

async def current_user(token: Optional[str] = Depends(oauth2)) -> dict:
    """Обязательная авторизация. Возвращает payload access-токена или 401."""
    if not token:
        raise HTTPException(status_code=401, detail="Необходима авторизация")
    return _decode(token, "access")


async def optional_user(token: Optional[str] = Depends(oauth2)) -> Optional[dict]:
    """
    Необязательная авторизация.
    Возвращает payload если токен валиден, иначе None.
    """
    if not token:
        return None
    try:
        return _decode(token, "access")
    except HTTPException:
        return None


# ИСПРАВЛЕНО: кешируем фабрику через lru_cache — один и тот же набор ролей
# возвращает одну и ту же функцию-зависимость, а не создаёт новую при каждом вызове
@lru_cache(maxsize=16)
def require_role(*roles: str):
    """
    Фабрика зависимостей для проверки роли.
    Выбрасывает HTTP 403 если роль пользователя не входит в список.
    """
    async def _check(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return _check


# Готовые зависимости
any_user     = Depends(current_user)
editors      = Depends(require_role("editor", "admin"))
admins       = Depends(require_role("admin"))
contributors = Depends(require_role("contributor", "editor", "admin"))


def _db_error(exc: Exception) -> HTTPException:
    """Логирует исключение БД и возвращает унифицированный HTTP 500."""
    logger.error("DB error: %s", exc, exc_info=True)
    return HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


# ---------------------------------------------------------------------------
# Роутер: аутентификация (/auth/*)
# ---------------------------------------------------------------------------

auth_router = APIRouter(prefix="/auth", tags=["Аутентификация"])


@auth_router.post("/login", response_model=TokenPair,
                  summary="Логин — получить пару токенов")
@limiter.limit("10/minute")
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Принимает username и password (application/x-www-form-urlencoded).
    Лимит: 10 запросов/мин на пользователя или IP.
    """
    row = await db.execute(select(UserORM).where(UserORM.username == form.username))
    user = row.scalars().first()

    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверные учётные данные")

    return _make_token_pair(user.username, user.role)


@auth_router.post("/refresh", response_model=TokenPair,
                  summary="Обновить токены по refresh-токену")
@limiter.limit("20/minute")
async def refresh(request: Request, body: RefreshRequest):
    """
    Принимает валидный refresh-токен, возвращает новую пару токенов.
    Старый refresh-токен остаётся валидным до истечения TTL
    (blacklist-инвалидация не реализована).
    """
    payload = _decode(body.refresh_token, "refresh")
    return _make_token_pair(payload["sub"], payload["role"])


@auth_router.post("/register", response_model=UserRead, status_code=201,
                  dependencies=[admins],
                  summary="Создать пользователя (только admin)")
async def register(user: UserCreate, db: AsyncSession = Depends(get_db)):
    """Создаёт пользователя. Только для администраторов. 409 если username занят."""
    existing = await db.execute(select(UserORM).where(UserORM.username == user.username))
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail="Пользователь уже существует")

    new_user = UserORM(
        username=user.username,
        password_hash=hash_password(user.password),
        role=user.role,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user


# ---------------------------------------------------------------------------
# Роутер: пользователи (/users/*)
# ---------------------------------------------------------------------------

users_router = APIRouter(prefix="/users", tags=["Пользователи"])


@users_router.get("/me", response_model=UserRead,
                  summary="Профиль текущего пользователя")
async def get_my_profile(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает профиль с кармой. Удобно для шапки приложения."""
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return orm_user


@users_router.patch("/me", response_model=UserRead,
                    summary="Обновить имя/фамилию текущего пользователя")
async def update_profile(
    body: UserUpdate,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обновляет имя и/или фамилию. Роль и пароль через этот эндпоинт не меняются."""
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(orm_user, field, value)

    await db.commit()
    await db.refresh(orm_user)
    return orm_user


# ---------------------------------------------------------------------------
# Роутер: публичные эндпоинты (/public/*) — авторизация не требуется
# ---------------------------------------------------------------------------

public_router = APIRouter(prefix="/public", tags=["Публичные"])


@public_router.get("/schedule/{weekday}", response_model=ScheduleResponse,
                   summary="Расписание на конкретный день недели")
@limiter.limit("120/minute")
async def public_get_schedule_by_day(
    request: Request,
    weekday: int = Path(..., ge=0, le=6,
                        description="День недели: 0=понедельник, 6=воскресенье"),
    class_name: Optional[str] = Query(None, max_length=30,
                                      description="Фильтр по классу, например '10A'"),
    db: AsyncSession = Depends(get_db),
):
    """
    Расписание на указанный день без авторизации.
    Результат отсортирован по классу, затем по номеру урока.

    GET /public/schedule/0              → всё расписание на понедельник
    GET /public/schedule/0?class_name=10A → расписание 10А на понедельник
    """
    try:
        # ИСПРАВЛЕНО: фильтр по class_name теперь в правильном месте — до order_by
        query = select(ScheduleORM).where(ScheduleORM.weekday == weekday)
        if class_name:
            query = query.where(ScheduleORM.class_name == class_name)
        query = query.order_by(ScheduleORM.class_name, ScheduleORM.lesson_num)
        rows = await db.execute(query)
        return ScheduleResponse(success=True, data=rows.scalars().all())
    except Exception as exc:
        raise _db_error(exc) from exc


@public_router.get("/olympiads", response_model=OlympiadResponse,
                   summary="Список олимпиад с фильтрацией")
@limiter.limit("120/minute")
async def public_get_olympiads(
    request: Request,
    date_start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                   description="Фильтр по дате: YYYY-MM-DD"),
    subject:    Optional[str] = Query(None, max_length=100, description="Предмет"),
    level:      Optional[int] = Query(None, ge=1, le=3,     description="Уровень 1-3"),
    classes:    Optional[str] = Query(None, max_length=100, description="Классы, например '9-11'"),
    db: AsyncSession = Depends(get_db),
):
    """
    Список олимпиад без авторизации. Все фильтры опциональны.
    Результат отсортирован по дате.
    """
    try:
        query = select(OlympiadORM).order_by(OlympiadORM.date_start)
        if date_start:
            query = query.where(OlympiadORM.date_start == date_start)
        if subject:
            query = query.where(OlympiadORM.subject == subject)
        if level is not None:
            query = query.where(OlympiadORM.level == level)
        if classes:
            query = query.where(OlympiadORM.classes == classes)
        rows = await db.execute(query)
        return OlympiadResponse(success=True, data=rows.scalars().all())
    except Exception as exc:
        raise _db_error(exc) from exc


@public_router.get("/olympiads/{olympiad_id}", response_model=OlympiadResponse,
                   summary="Одна олимпиада по ID")
@limiter.limit("120/minute")
async def public_get_olympiad(
    request: Request,
    olympiad_id: int = Path(..., ge=1, description="ID олимпиады"),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает одну олимпиаду по ID без авторизации."""
    row = await db.get(OlympiadORM, olympiad_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Олимпиада не найдена")
    return OlympiadResponse(success=True, data=[row])


# ---------------------------------------------------------------------------
# Роутер: олимпиады (/olympiads) — требуют авторизации
# ---------------------------------------------------------------------------

olympiads_router = APIRouter(prefix="/olympiads", tags=["Олимпиады"])


@olympiads_router.post("", response_model=OlympiadResponse, status_code=201,
                        dependencies=[editors],
                        summary="Создать олимпиаду (editor+)")
@limiter.limit("20/minute")
async def create_olympiad(
    request: Request,
    olympiad: OlympiadBase,
    db: AsyncSession = Depends(get_db),
):
    try:
        new_row = OlympiadORM(**olympiad.model_dump())
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        return OlympiadResponse(success=True, data=[new_row])
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@olympiads_router.put("/{olympiad_id}", response_model=OlympiadResponse,
                       dependencies=[editors],
                       summary="Полное обновление олимпиады (editor+)")
@limiter.limit("20/minute")
async def update_olympiad(
    request: Request,
    olympiad_id: int = Path(..., ge=1),
    olympiad: OlympiadBase = ...,
    db: AsyncSession = Depends(get_db),
):
    """Заменяет все поля олимпиады. Для частичного обновления используйте PATCH."""
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        for field, value in olympiad.model_dump().items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return OlympiadResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@olympiads_router.patch("/{olympiad_id}", response_model=OlympiadResponse,
                         dependencies=[editors],
                         summary="Частичное обновление олимпиады (editor+)")
@limiter.limit("20/minute")
async def patch_olympiad(
    request: Request,
    olympiad_id: int = Path(..., ge=1),
    olympiad: OlympiadUpdate = ...,
    db: AsyncSession = Depends(get_db),
):
    """Обновляет только переданные поля."""
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        for field, value in olympiad.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return OlympiadResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@olympiads_router.delete("/{olympiad_id}", response_model=OlympiadResponse,
                          dependencies=[admins],
                          summary="Удалить олимпиаду (только admin)")
@limiter.limit("20/minute")
async def delete_olympiad(
    request: Request,
    olympiad_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        await db.delete(row)
        await db.commit()
        return OlympiadResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


# ---------------------------------------------------------------------------
# Роутер: расписание (/schedule) — требуют авторизации
# ---------------------------------------------------------------------------

schedule_router = APIRouter(prefix="/schedule", tags=["Расписание"])


@schedule_router.post("", response_model=ScheduleResponse, status_code=201,
                       dependencies=[editors],
                       summary="Создать урок (editor+)")
@limiter.limit("2000/minute")
async def create_lesson(
    request: Request,
    lesson: ScheduleBase,
    db: AsyncSession = Depends(get_db),
):
    try:
        new_row = ScheduleORM(**lesson.model_dump())
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        return ScheduleResponse(success=True, data=[new_row])
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@schedule_router.put("/{lesson_id}", response_model=ScheduleResponse,
                      dependencies=[editors],
                      summary="Полное обновление урока (editor+)")
@limiter.limit("20/minute")
async def update_lesson(
    request: Request,
    lesson_id: int = Path(..., ge=1),
    lesson: ScheduleBase = ...,
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Урок не найден")
        for field, value in lesson.model_dump().items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return ScheduleResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@schedule_router.patch("/{lesson_id}", response_model=ScheduleResponse,
                        dependencies=[editors],
                        summary="Частичное обновление урока (editor+)")
@limiter.limit("20/minute")
async def patch_lesson(
    request: Request,
    lesson_id: int = Path(..., ge=1),
    lesson: ScheduleUpdate = ...,
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Урок не найден")
        for field, value in lesson.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return ScheduleResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@schedule_router.delete("/{lesson_id}", response_model=ScheduleResponse,
                         dependencies=[admins],
                         summary="Удалить урок (только admin)")
@limiter.limit("20/minute")
async def delete_lesson(
    request: Request,
    lesson_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Урок не найден")
        await db.delete(row)
        await db.commit()
        return ScheduleResponse(success=True, data=[row])
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


# ---------------------------------------------------------------------------
# Роутер: предложения изменений (/proposals) — contributor workflow
# ---------------------------------------------------------------------------

proposals_router = APIRouter(prefix="/proposals", tags=["Предложения изменений"])


@proposals_router.post("", response_model=ProposalResponse, status_code=201,
                        summary="Предложить изменение (contributor+)")
@limiter.limit("30/minute")
async def create_proposal(
    request: Request,
    body: ProposalCreate,
    user: dict = Depends(require_role("contributor", "editor", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт предложение со статусом pending.
    Изменение применяется только после одобрения через /proposals/{id}/review.
    """
    if body.action in ("update", "delete") and body.entity_id is None:
        raise HTTPException(status_code=422, detail="entity_id обязателен для update/delete")
    if body.action == "create" and body.entity_id is not None:
        raise HTTPException(status_code=422, detail="entity_id должен быть None для create")

    now = datetime.now(timezone.utc).isoformat()
    new_proposal = ProposalORM(
        author=user["sub"],
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        action=body.action,
        payload=_json.dumps(body.payload, ensure_ascii=False),
        status="pending",
        created_at=now,
    )
    try:
        db.add(new_proposal)
        await db.commit()
        await db.refresh(new_proposal)
        return ProposalResponse(success=True, data=[ProposalRead.from_orm(new_proposal)])
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


@proposals_router.get("", response_model=ProposalResponse,
                       summary="Список всех предложений (editor+)")
@limiter.limit("60/minute")
async def list_proposals(
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status",
                                          pattern=r"^(pending|approved|rejected)$"),
    entity_type:   Optional[str] = Query(None, pattern=r"^(olympiad|schedule)$"),
    user: dict = Depends(require_role("editor", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Все предложения, новые сначала. Только для editor и admin.
    Фильтры: ?status=pending, ?entity_type=olympiad.
    """
    try:
        query = select(ProposalORM).order_by(ProposalORM.created_at.desc())
        if status_filter:
            query = query.where(ProposalORM.status == status_filter)
        if entity_type:
            query = query.where(ProposalORM.entity_type == entity_type)
        rows = await db.execute(query)
        proposals = [ProposalRead.from_orm(r) for r in rows.scalars().all()]
        return ProposalResponse(success=True, data=proposals)
    except Exception as exc:
        raise _db_error(exc) from exc


@proposals_router.get("/my", response_model=ProposalResponse,
                       summary="Мои предложения (любой авторизованный)")
@limiter.limit("60/minute")
async def my_proposals(
    request: Request,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Предложения текущего пользователя, новые сначала."""
    try:
        query = (
            select(ProposalORM)
            .where(ProposalORM.author == user["sub"])
            .order_by(ProposalORM.created_at.desc())
        )
        rows = await db.execute(query)
        proposals = [ProposalRead.from_orm(r) for r in rows.scalars().all()]
        return ProposalResponse(success=True, data=proposals)
    except Exception as exc:
        raise _db_error(exc) from exc


@proposals_router.get("/{proposal_id}", response_model=ProposalResponse,
                       summary="Одно предложение по ID (contributor — только своё, editor+ — любое)")
@limiter.limit("60/minute")
async def get_proposal(
    request: Request,
    proposal_id: int = Path(..., ge=1),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")

    if user["role"] not in ("editor", "admin") and proposal.author != user["sub"]:
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    return ProposalResponse(success=True, data=[ProposalRead.from_orm(proposal)])


@proposals_router.post("/{proposal_id}/review", response_model=ProposalResponse,
                        summary="Одобрить или отклонить предложение (editor+)")
@limiter.limit("30/minute")
async def review_proposal(
    request: Request,
    proposal_id: int = Path(..., ge=1),
    body: ProposalReview = ...,
    user: dict = Depends(require_role("editor", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Рецензирование предложения.

    approved → изменение применяется к БД, карма автора +1.
    rejected → изменение не применяется, карма автора -1.
              Можно указать review_note с причиной отклонения.

    Повторная рецензия уже рассмотренного предложения → HTTP 409.
    """
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="Предложение уже рассмотрено")

    proposal.status      = body.decision
    proposal.reviewed_by = user["sub"]
    proposal.review_note = body.review_note

    # Обновляем карму автора
    karma_delta = +1 if body.decision == "approved" else -1
    author_row = await db.execute(
        select(UserORM).where(UserORM.username == proposal.author)
    )
    author = author_row.scalars().first()
    if author is not None:
        author.karma += karma_delta

    if body.decision == "approved":
        payload  = _json.loads(proposal.payload)
        orm_map  = {"olympiad": OlympiadORM, "schedule": ScheduleORM}
        OrmClass = orm_map.get(proposal.entity_type)
        if OrmClass is None:
            raise HTTPException(status_code=422, detail="Неизвестный тип сущности")

        try:
            if proposal.action == "create":
                db.add(OrmClass(**payload))

            elif proposal.action == "update":
                row = await db.get(OrmClass, proposal.entity_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="Исходная запись не найдена")
                for field, value in payload.items():
                    setattr(row, field, value)

            elif proposal.action == "delete":
                row = await db.get(OrmClass, proposal.entity_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="Исходная запись не найдена")
                await db.delete(row)

        except HTTPException:
            raise
        except Exception as exc:
            await db.rollback()
            raise _db_error(exc) from exc

    try:
        await db.commit()
        await db.refresh(proposal)
        return ProposalResponse(success=True, data=[ProposalRead.from_orm(proposal)])
    except Exception as exc:
        await db.rollback()
        raise _db_error(exc) from exc


# ---------------------------------------------------------------------------
# Регистрация роутеров
# ---------------------------------------------------------------------------

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(public_router)
app.include_router(olympiads_router)
app.include_router(schedule_router)
app.include_router(proposals_router)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="localhost", port=1717, reload=IS_DEV)