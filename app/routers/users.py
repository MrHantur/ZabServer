# app/routers/users.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from app import schemas, dependencies
from app.database import SessionLocal
from app.models import UserORM
from app.core import limiter

router = APIRouter(prefix="/users", tags=["Пользователи"])

@router.get("/me", response_model=schemas.UserRead, summary="Профиль")
async def get_my_profile(user: dict = Depends(dependencies.current_user), db: SessionLocal = Depends(dependencies.get_db)):
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return orm_user

@router.patch("/me", response_model=schemas.UserRead, summary="Обновить профиль")
async def update_profile(body: schemas.UserUpdate, user: dict = Depends(dependencies.current_user), db: SessionLocal = Depends(dependencies.get_db)):
    row = await db.execute(select(UserORM).where(UserORM.username == user["sub"]))
    orm_user = row.scalars().first()
    if orm_user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(orm_user, field, value)
    await db.commit()
    await db.refresh(orm_user)
    return orm_user