"""
Комменты писала нейросеть, за их качество не ручаюсь
Но вроде полного бреда нету
"""

"""
Zab API — бэкенд для школьного портала.
Версия 1.2.0

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
from typing import AsyncGenerator, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from bcrypt import gensalt, hashpw, checkpw

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

# URL базы данных. По умолчанию SQLite с асинхронным драйвером aiosqlite.
# Для PostgreSQL используйте: postgresql+asyncpg://user:pass@host/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./zabdata.db")

# Если ENV=development — включаем отладочный вывод SQL-запросов через echo
IS_DEV = os.getenv("ENV", "development") == "development"

# Секретный ключ для подписи JWT. ОБЯЗАТЕЛЬНО менять в продакшене!
JWT_SECRET        = os.getenv("JWT_SECRET", "PLACEHOLDER")
JWT_ALGORITHM     = "HS256"                           # Алгоритм подписи токена
ACCESS_TOKEN_TTL  = int(os.getenv("ACCESS_TOKEN_TTL",  "30"))     # минуты
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL", "43200"))  # минуты (30 дней)

# Асинхронный движок SQLAlchemy. echo=IS_DEV логирует SQL только в dev-режиме.
engine       = create_async_engine(DATABASE_URL, echo=IS_DEV)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Функции для хеширования и проверки паролей
def hash_password(password: str) -> str:
    salt = gensalt()
    return hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(plain: str, hashed: str) -> bool:
    return checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

# OAuth2-схема: токен берётся из заголовка Authorization: Bearer <token>.
# auto_error=False — не выбрасывает 401 автоматически; это позволяет
# реализовывать необязательную авторизацию через зависимость optional_user.
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

    Карма:
      Увеличивается на 1 при одобрении предложения, уменьшается на 1 при отклонении.
    """
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String,  unique=True, nullable=False, index=True)
    password_hash = Column(String,  nullable=False)
    role          = Column(String,  nullable=False, default="viewer")
    first_name    = Column(String,  nullable=True)   # Имя пользователя (опционально)
    last_name     = Column(String,  nullable=True)   # Фамилия пользователя (опционально)
    karma         = Column(Integer, nullable=False, default=0)  # Карма пользователя


class OlympiadORM(Base):
    """Олимпиада."""
    __tablename__ = "olympiads"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String,  nullable=False)
    description = Column(String,  nullable=True)
    subject     = Column(String,  nullable=False)               # Предмет (индекс для быстрой фильтрации)
    date        = Column(String,  nullable=False)               # Дата проведения: YYYY-MM-DD
    time        = Column(String,  nullable=True)
    classes     = Column(String,  nullable=False)               # Классы-участники, например "9-11"
    stage       = Column(String,  nullable=True)
    level       = Column(Integer, nullable=True)                # Уровень
    link        = Column(String,  nullable=True)                # Ссылка на материалы (опционально)


class ScheduleORM(Base):
    """Урок в расписании."""
    __tablename__ = "schedule"

    id          = Column(Integer, primary_key=True, index=True)
    class_name  = Column(String,  nullable=False, index=True)  # Например, "10A"
    weekday     = Column(Integer, nullable=False, index=True)  # 0=пн, 1=вт, ..., 6=вс
    lesson_num  = Column(Integer, nullable=False)               # Номер урока: 1..20
    subject     = Column(String,  nullable=False)
    teacher     = Column(String,  nullable=True)
    room        = Column(String,  nullable=True)
    time_start  = Column(String,  nullable=True)               # Время начала: HH:MM
    time_end    = Column(String,  nullable=True)               # Время окончания: HH:MM


class ProposalORM(Base):
    """
    Предложение изменения от contributor.

    Жизненный цикл записи: pending → approved | rejected.
    При статусе approved изменение применяется к БД автоматически
    в обработчике POST /proposals/{id}/review.
    """
    __tablename__ = "proposals"

    id          = Column(Integer, primary_key=True, index=True)
    author      = Column(String,  nullable=False, index=True)    # username автора предложения
    entity_type = Column(String,  nullable=False)                # "olympiad" | "schedule"
    entity_id   = Column(Integer, nullable=True)                 # NULL → предложение создать новую запись
    action      = Column(String,  nullable=False)                # "create" | "update" | "delete"
    payload     = Column(String,  nullable=False)                # JSON-строка с данными изменения
    status      = Column(String,  nullable=False, default="pending")  # pending | approved | rejected
    created_at  = Column(String,  nullable=False)                # ISO datetime UTC
    reviewed_by = Column(String,  nullable=True)                 # username рецензента
    review_note = Column(String,  nullable=True)                 # Комментарий при отклонении


# ---------------------------------------------------------------------------
# Pydantic-схемы (DTO)
# ---------------------------------------------------------------------------

# Переиспользуемые Field-ограничения для единообразной валидации
_NAME   = Field(..., min_length=1, max_length=200)
_SHORT  = Field(..., min_length=1, max_length=100)
_OPT30  = Field(None, max_length=30)
_OPT100 = Field(None, max_length=100)
_OPT500 = Field(None, max_length=500)
_TIME   = Field(None, pattern=r"^\d{2}:\d{2}$")   # Формат HH:MM

# --- Аутентификация и пользователи ---

class TokenPair(BaseModel):
    """Пара токенов, возвращаемая при логине и обновлении."""
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"


class RefreshRequest(BaseModel):
    """Тело запроса на обновление токенов."""
    refresh_token: str


class UserCreate(BaseModel):
    """Данные для создания нового пользователя (только для admin)."""
    username:   str           = Field(..., min_length=3, max_length=50)
    password:   str           = Field(..., min_length=8, max_length=128)
    role:       str           = Field("viewer", pattern=r"^(viewer|contributor|editor|admin)$")
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name:  Optional[str] = Field(None, min_length=1, max_length=100)


class UserRead(BaseModel):
    """Публичное представление пользователя (без пароля)."""
    id:         int
    username:   str
    role:       str
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    karma:      int = 0
    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    """Обновление профиля: только имя и фамилия. Роль/пароль не меняются."""
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name:  Optional[str] = Field(None, min_length=1, max_length=100)


# --- Олимпиады ---

class OlympiadBase(BaseModel):
    """Базовая схема олимпиады — для создания и полного обновления (PUT)."""
    name:        str           = _NAME
    description: str           = _OPT500
    subject:     str           = _SHORT
    date:        str           = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time:        str           = _TIME
    classes:     str           = Field(..., pattern=r"^(?:[1-9]|10|11)-(?:[1-9]|10|11)$")
    stage:       str           = _OPT30
    level:       int           = Field(None, ge=1, le=3)
    link:        Optional[str] = _OPT500


class OlympiadRead(OlympiadBase):
    """Схема чтения олимпиады (добавляет id)."""
    id: int
    model_config = {"from_attributes": True}


class OlympiadUpdate(BaseModel):
    """Схема частичного обновления олимпиады (все поля опциональны, для PATCH)."""
    name:        Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    subject:     Optional[str] = Field(None, min_length=1, max_length=100)
    date:        Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time:        Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    classes:     Optional[str] = Field(None, min_length=1, max_length=100)
    level:       Optional[int] = Field(None, ge=1, le=3)
    link:        Optional[str] = Field(None, max_length=500)


class OlympiadResponse(BaseModel):
    """Стандартный ответ для эндпоинтов олимпиад."""
    success: bool
    data:    list[OlympiadRead]
    error:   Optional[str] = None


# --- Расписание ---

class ScheduleBase(BaseModel):
    """Базовая схема урока — для создания и полного обновления (PUT)."""
    class_name:  str           = Field(..., min_length=2, max_length=3)
    weekday:     int           = Field(..., ge=0, le=6)   # 0=пн, 6=вс
    lesson_num:  int           = Field(..., ge=1, le=20)
    subject:     str           = _SHORT
    teacher:     Optional[str] = _OPT100
    room:        Optional[str] = _OPT30
    time_start:  Optional[str] = _TIME
    time_end:    Optional[str] = _TIME


class ScheduleRead(ScheduleBase):
    """Схема чтения урока (добавляет id)."""
    id: int
    model_config = {"from_attributes": True}


class ScheduleUpdate(BaseModel):
    """Схема частичного обновления урока (все поля опциональны, для PATCH)."""
    class_name:  Optional[str] = Field(None, in_length=2, max_length=3)
    weekday:     Optional[int] = Field(None, ge=0, le=6)
    lesson_num:  Optional[int] = Field(None, ge=1, le=20)
    subject:     Optional[str] = Field(None, min_length=1, max_length=100)
    teacher:     Optional[str] = Field(None, max_length=100)
    room:        Optional[str] = Field(None, max_length=30)
    time_start:  Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    time_end:    Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class ScheduleResponse(BaseModel):
    """Стандартный ответ для эндпоинтов расписания."""
    success: bool
    data:    list[ScheduleRead]
    error:   Optional[str] = None


# --- Предложения изменений ---

class ProposalCreate(BaseModel):
    """
    Тело запроса на создание предложения изменения.

    Правила согласованности action/entity_id:
      - action=create  → entity_id должен быть None; payload — полные данные новой записи
      - action=update  → entity_id обязателен; payload — изменяемые поля (как в PATCH)
      - action=delete  → entity_id обязателен; payload может быть пустым {}
    """
    entity_type: str           = Field(..., pattern=r"^(olympiad|schedule)$")
    entity_id:   Optional[int] = None
    action:      str           = Field(..., pattern=r"^(create|update|delete)$")
    payload:     dict


class ProposalReview(BaseModel):
    """Тело запроса при рецензировании предложения (editor/admin)."""
    decision:    str           = Field(..., pattern=r"^(approved|rejected)$")
    review_note: Optional[str] = Field(None, max_length=500)


class ProposalRead(BaseModel):
    """Публичное представление предложения изменения."""
    id:          int
    author:      str
    entity_type: str
    entity_id:   Optional[int]
    action:      str
    payload:     dict           # Десериализованный JSON из БД
    status:      str
    created_at:  str
    reviewed_by: Optional[str] = None
    review_note: Optional[str] = None

    @classmethod
    def from_orm(cls, row: ProposalORM) -> "ProposalRead":
        """Конвертирует ORM-объект в схему, десериализуя payload из JSON-строки."""
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
    """Стандартный ответ для эндпоинтов предложений."""
    success: bool
    data:    list[ProposalRead]
    error:   Optional[str] = None


# ---------------------------------------------------------------------------
# FastAPI — инициализация приложения
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения.
    При старте создаёт все таблицы в БД (если их ещё нет).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _token_key(request: Request) -> str:
    """
    Функция формирования ключа для rate-limiter (slowapi).
    Если запрос содержит валидный Bearer-токен — ключ строится по sub (username),
    иначе — по IP-адресу клиента. Срок действия токена при этом не проверяется.
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


# Инициализация rate-limiter с кастомным ключом
limiter = Limiter(key_func=_token_key)

app = FastAPI(title="Zab API", version="1.2.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency Injection: выдаёт асинхронную сессию БД для одного запроса.
    Сессия автоматически закрывается после завершения обработчика.
    """
    async with SessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# JWT-утилиты
# ---------------------------------------------------------------------------

def _make_token(sub: str, role: str, ttl_minutes: int, kind: str) -> str:
    """
    Создаёт и подписывает JWT.

    Payload:
      sub  — идентификатор пользователя (username)
      role — роль пользователя
      kind — тип токена: "access" или "refresh"
      iat  — время создания (UTC)
      exp  — время истечения (UTC)
    """
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
    """Создаёт пару access + refresh токенов для пользователя."""
    return TokenPair(
        access_token=_make_token(username, role, ACCESS_TOKEN_TTL,  "access"),
        refresh_token=_make_token(username, role, REFRESH_TOKEN_TTL, "refresh"),
    )


def _decode(token: str, expected_kind: str) -> dict:
    """
    Декодирует и валидирует JWT.
    Проверяет подпись, срок действия и тип токена (kind).
    Выбрасывает HTTPException 401 при любой ошибке валидации.
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
    """
    Обязательная авторизация.
    Возвращает payload из access-токена или выбрасывает HTTP 401.
    Используется во всех защищённых эндпоинтах через Depends(current_user).
    """
    if not token:
        raise HTTPException(status_code=401, detail="Необходима авторизация")
    return _decode(token, "access")


async def optional_user(token: Optional[str] = Depends(oauth2)) -> Optional[dict]:
    """
    Необязательная авторизация.
    Возвращает payload если токен передан и валиден, иначе None.
    Используется там, где публичный доступ разрешён, но авторизованный
    пользователь может получить дополнительные данные.
    """
    if not token:
        return None
    try:
        return _decode(token, "access")
    except HTTPException:
        return None


def require_role(*roles: str):
    """
    Фабрика зависимостей для проверки роли пользователя.
    Выбрасывает HTTP 403 если роль не входит в список разрешённых.

    Пример использования:
      dependencies=[Depends(require_role("editor", "admin"))]
    """
    async def _check(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return _check


# Готовые зависимости для удобного использования в декораторах
any_user     = Depends(current_user)                                    # любой авторизованный
editors      = Depends(require_role("editor", "admin"))                 # editor и admin
admins       = Depends(require_role("admin"))                           # только admin
contributors = Depends(require_role("contributor", "editor", "admin"))  # contributor и выше


def _db_error(exc: Exception) -> HTTPException:
    """Логирует неожиданное исключение БД и возвращает унифицированный HTTP 500."""
    logger.error("DB error: %s", exc, exc_info=True)
    return HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


# ---------------------------------------------------------------------------
# Эндпоинты аутентификации (/auth/*)
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenPair,
          summary="Логин — получить пару токенов")
@limiter.limit("10/minute")
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Принимает username и password (application/x-www-form-urlencoded).
    Возвращает access_token (TTL: ACCESS_TOKEN_TTL мин) и refresh_token.
    Лимит: 10 запросов/мин на пользователя или IP.
    """
    row = await db.execute(select(UserORM).where(UserORM.username == form.username))
    user = row.scalars().first()

    # Константное время проверки предотвращает timing-атаки (bcrypt)
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверные учётные данные")

    return _make_token_pair(user.username, user.role)


@app.post("/auth/refresh", response_model=TokenPair,
          summary="Обновить токены по refresh-токену")
@limiter.limit("20/minute")
async def refresh(request: Request, body: RefreshRequest):
    """
    Принимает валидный refresh-токен.
    Возвращает новую пару access + refresh токенов.
    Старый refresh-токен остаётся валидным до истечения TTL
    (blacklist-инвалидация в текущей версии не реализована).
    """
    payload = _decode(body.refresh_token, "refresh")
    return _make_token_pair(payload["sub"], payload["role"])


@app.post("/auth/register", response_model=UserRead, status_code=201,
          dependencies=[admins], summary="Создать пользователя (только admin)")
async def register(user: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Создаёт нового пользователя. Доступно только администраторам.
    Возвращает HTTP 409 если username уже занят.
    """
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
# Эндпоинты пользователей (/users/*)
# ---------------------------------------------------------------------------

@app.get("/users/me", response_model=UserRead,
         summary="Профиль текущего пользователя (с кармой)")
async def get_my_profile(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает профиль текущего авторизованного пользователя, включая карму.
    Удобно для отображения в шапке мобильного приложения.
    """
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return orm_user


@app.patch("/users/me", response_model=UserRead,
           summary="Обновить имя/фамилию текущего пользователя")
async def update_profile(
    body: UserUpdate,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Позволяет любому авторизованному пользователю обновить своё имя и/или фамилию.
    Изменение роли или пароля через этот эндпоинт недоступно.
    """
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # exclude_unset=True — обновляем только явно переданные поля
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(orm_user, field, value)

    await db.commit()
    await db.refresh(orm_user)
    return orm_user


# ---------------------------------------------------------------------------
# Публичные эндпоинты (/public/*) — авторизация НЕ требуется
# ---------------------------------------------------------------------------

@app.get("/public/schedule/{weekday}", response_model=ScheduleResponse,
         summary="[Публично] Расписание на конкретный день недели")
@limiter.limit("120/minute")
async def public_get_schedule_by_day(
    request: Request,
    # Path(...) — значение берётся из сегмента URL: /public/schedule/1
    # ВАЖНО: для path-параметров необходимо использовать Path(), а не Query()
    weekday: int = Path(..., ge=0, le=6,
                        description="День недели: 0=понедельник, 6=воскресенье"),
    # Query-параметр — опциональный, передаётся как ?class_name=10A
    class_name: Optional[str] = Query(None, max_length=20,
                                      description="Фильтр по классу, например '10A'"),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает расписание на указанный день недели без авторизации.
    Результат отсортирован по классу, затем по номеру урока.

    Примеры:
      GET /public/schedule/0              → всё расписание на понедельник
      GET /public/schedule/0?class_name=10A → расписание 10А на понедельник
    """
    try:
        query = (
            select(ScheduleORM)
            .where(ScheduleORM.weekday == weekday)
            .order_by(ScheduleORM.class_name, ScheduleORM.lesson_num)
        )
        if class_name:
            query = query.where(ScheduleORM.class_name == class_name)
        rows = await db.execute(query)
        return ScheduleResponse(success=True, data=rows.scalars().all())
    except Exception as exc:
        raise _db_error(exc) from exc


@app.get("/public/olympiads", response_model=OlympiadResponse,
         summary="[Публично] Список олимпиад с фильтрацией")
@limiter.limit("120/minute")
async def public_get_olympiads(
    request: Request,
    date:    Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                   description="Фильтр по дате: YYYY-MM-DD"),
    subject: Optional[str] = Query(None, max_length=100, description="Предмет"),
    level:   Optional[int] = Query(None, ge=1, le=3,     description="Уровень 1-3"),
    classes: Optional[str] = Query(None, max_length=100, description="Классы, например '9-11'"),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает список олимпиад без авторизации.
    Все параметры фильтрации опциональны. Результат отсортирован по дате.

    Пример: GET /public/olympiads?date=2025-03-15&level=2
    """
    try:
        query = select(OlympiadORM).order_by(OlympiadORM.date)
        if date:
            query = query.where(OlympiadORM.date == date)
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


@app.get("/public/olympiads/{olympiad_id}", response_model=OlympiadResponse,
         summary="[Публично] Одна олимпиада по ID")
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
# Эндпоинты олимпиад (/olympiads) — требуют авторизации
# ---------------------------------------------------------------------------

@app.post("/olympiads", response_model=OlympiadResponse, status_code=201,
          dependencies=[editors], summary="Создать олимпиаду (editor+)")
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


@app.put("/olympiads/{olympiad_id}", response_model=OlympiadResponse,
         dependencies=[editors], summary="Полное обновление олимпиады (editor+)")
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


@app.patch("/olympiads/{olympiad_id}", response_model=OlympiadResponse,
           dependencies=[editors], summary="Частичное обновление олимпиады (editor+)")
@limiter.limit("20/minute")
async def patch_olympiad(
    request: Request,
    olympiad_id: int = Path(..., ge=1),
    olympiad: OlympiadUpdate = ...,
    db: AsyncSession = Depends(get_db),
):
    """Обновляет только переданные поля (exclude_unset=True)."""
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


@app.delete("/olympiads/{olympiad_id}", response_model=OlympiadResponse,
            dependencies=[admins], summary="Удалить олимпиаду (только admin)")
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
# Эндпоинты расписания (/schedule) — требуют авторизации
# ---------------------------------------------------------------------------

@app.post("/schedule", response_model=ScheduleResponse, status_code=201,
          dependencies=[editors], summary="Создать урок (editor+)")
@limiter.limit("20/minute")
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


@app.put("/schedule/{lesson_id}", response_model=ScheduleResponse,
         dependencies=[editors], summary="Полное обновление урока (editor+)")
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


@app.patch("/schedule/{lesson_id}", response_model=ScheduleResponse,
           dependencies=[editors], summary="Частичное обновление урока (editor+)")
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


@app.delete("/schedule/{lesson_id}", response_model=ScheduleResponse,
            dependencies=[admins], summary="Удалить урок (только admin)")
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
# Эндпоинты предложений изменений (/proposals) — contributor workflow
# ---------------------------------------------------------------------------

@app.post("/proposals", response_model=ProposalResponse, status_code=201,
          summary="Предложить изменение (contributor+)")
@limiter.limit("30/minute")
async def create_proposal(
    request: Request,
    body: ProposalCreate,
    user: dict = Depends(require_role("contributor", "editor", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт предложение изменения со статусом pending.
    Само изменение НЕ применяется — только после одобрения через /proposals/{id}/review.
    Contributor, editor и admin могут использовать этот эндпоинт.
    """
    # Дополнительная валидация согласованности action и entity_id
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


@app.get("/proposals", response_model=ProposalResponse,
         summary="Список всех предложений (editor+)")
@limiter.limit("60/minute")
async def list_proposals(
    request: Request,
    # Используем alias="status", чтобы не конфликтовать с встроенным
    # атрибутом status в Python, но принимать параметр как ?status=pending
    status_filter: Optional[str] = Query(None, alias="status",
                                          pattern=r"^(pending|approved|rejected)$"),
    entity_type:   Optional[str] = Query(None, pattern=r"^(olympiad|schedule)$"),
    user: dict = Depends(require_role("editor", "admin")),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает все предложения, отсортированные по дате (новые сначала).
    Доступно редакторам и администраторам.
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


@app.get("/proposals/my", response_model=ProposalResponse,
         summary="Мои предложения (любой авторизованный)")
@limiter.limit("60/minute")
async def my_proposals(
    request: Request,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает все предложения текущего пользователя (новые сначала).
    Доступно любому авторизованному пользователю.
    """
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


@app.get("/proposals/{proposal_id}", response_model=ProposalResponse,
         summary="Одно предложение по ID (contributor видит только своё, editor+ — любое)")
@limiter.limit("60/minute")
async def get_proposal(
    request: Request,
    proposal_id: int = Path(..., ge=1),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает одно предложение по ID.
    Contributor может смотреть только свои предложения (проверка по author).
    Editor и admin видят любое предложение.
    """
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")

    # Contributor может смотреть только свои предложения
    if user["role"] not in ("editor", "admin") and proposal.author != user["sub"]:
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    return ProposalResponse(success=True, data=[ProposalRead.from_orm(proposal)])


@app.post("/proposals/{proposal_id}/review", response_model=ProposalResponse,
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
    Рецензирование предложения изменения.

    decision=approved:
      Статус → approved. Изменение автоматически применяется к БД:
        create → создаётся новая запись из payload
        update → обновляются поля записи (как PATCH)
        delete → запись удаляется
      Карма автора: +1

    decision=rejected:
      Статус → rejected. Изменение НЕ применяется.
      Можно указать review_note с причиной отклонения.
      Карма автора: -1

    Повторная рецензия уже рассмотренного предложения → HTTP 409.
    """
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="Предложение уже рассмотрено")

    # Обновляем поля рецензии
    proposal.status      = body.decision
    proposal.reviewed_by = user["sub"]
    proposal.review_note = body.review_note

    # Обновляем карму автора предложения
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
                # Создаём новую запись целиком из payload
                db.add(OrmClass(**payload))

            elif proposal.action == "update":
                row = await db.get(OrmClass, proposal.entity_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="Исходная запись не найдена")
                # Применяем только переданные поля (аналогично PATCH)
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
# Точка входа (python server.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="localhost", port=1717, reload=IS_DEV)