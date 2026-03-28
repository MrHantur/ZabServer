# app/routers/auth.py
from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models import UserORM
from app import schemas, dependencies, utils
from app.core import limiter

router = APIRouter(prefix="/auth", tags=["Аутентификация"])

async def _create_user(user_data: schemas.UserCreate, db: SessionLocal) -> UserORM:
    """Внутренняя функция для создания пользователя (DRI - Don't Repeat Yourself)"""
    # Проверяем, существует ли пользователь с таким username
    existing = await db.execute(select(UserORM).where(UserORM.username == user_data.username))
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким именем уже существует"
        )

    # Принудительно устанавливаем роль viewer при регистрации
    # Игнорируем роль из запроса, если она там была (для безопасности)
    new_user = UserORM(
        username=user_data.username,
        password_hash=utils.hash_password(user_data.password),
        role="viewer", 
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        karma=0
    )

    db.add(new_user)
    try:
        await db.commit()
        await db.refresh(new_user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким именем уже существует"
        )
    return new_user

@router.post("/login", response_model=schemas.TokenPair, summary="Логин")
@limiter.limit("10/minute")
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: SessionLocal = Depends(dependencies.get_db),
):
    row = await db.execute(select(UserORM).where(UserORM.username == form.username))
    user = row.scalars().first()
    if not user or not utils.verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверные учётные данные")
    return utils._make_token_pair(user.username, user.role)

@router.post("/refresh", response_model=schemas.TokenPair, summary="Обновить токены")
@limiter.limit("20/minute")
async def refresh(request: Request, body: schemas.RefreshRequest):
    payload = utils._decode(body.refresh_token, "refresh")
    return utils._make_token_pair(payload["sub"], payload["role"])

@router.post("/register", response_model=schemas.UserRead, status_code=status.HTTP_201_CREATED, summary="Регистрация")
@limiter.limit("5/minute")
async def register(
    request: Request,
    user: schemas.UserCreate,
    db: SessionLocal = Depends(dependencies.get_db)
):
    new_user = await _create_user(user, db)
    # Корректно преобразуем ORM объект в Pydantic схему
    return schemas.UserRead.model_validate(new_user)

@router.post("/register-with-token", response_model=schemas.TokenPair, status_code=status.HTTP_201_CREATED, summary="Регистрация + вход")
@limiter.limit("5/minute")
async def register_with_token(
    request: Request,
    user: schemas.UserCreate,
    db: SessionLocal = Depends(dependencies.get_db)
):
    new_user = await _create_user(user, db)
    # Возвращаем токены для автоматического входа
    return utils._make_token_pair(new_user.username, new_user.role)