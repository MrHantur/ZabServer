# app/routers/schedule.py
from fastapi import APIRouter, Depends, Request, Path, HTTPException
import json as _json
from app import schemas, dependencies
from app.database import SessionLocal
from app.models import ScheduleORM
from app.core import limiter

router = APIRouter(prefix="/schedule", tags=["Расписание"])

def _make_user_info(user: dict) -> str:
    """Сериализует информацию о пользователе в JSON-строку"""
    return _json.dumps({
        "username": user["sub"],
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", "")
    }, ensure_ascii=False)

@router.post("", response_model=schemas.ScheduleResponse, status_code=201, dependencies=[dependencies.editors], summary="Создать урок")
@limiter.limit("20/minute")
async def create_lesson(
    request: Request, 
    lesson: schemas.ScheduleBase, 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        data = lesson.model_dump()
        data['created_by'] = _make_user_info(user)
        data['approved_by'] = _make_user_info(user)
        
        new_row = ScheduleORM(**data)
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        return schemas.ScheduleResponse(success=True, data=[new_row])
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.put("/{lesson_id}", response_model=schemas.ScheduleResponse, dependencies=[dependencies.editors], summary="Обновить урок (PUT)")
@limiter.limit("20/minute")
async def update_lesson(
    request: Request, 
    lesson_id: int = Path(..., ge=1), 
    lesson: schemas.ScheduleBase = ..., 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Урок не найден")
        
        for field, value in lesson.model_dump().items(): 
            setattr(row, field, value)
        
        row.approved_by = _make_user_info(user)
        
        await db.commit()
        await db.refresh(row)
        return schemas.ScheduleResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.patch("/{lesson_id}", response_model=schemas.ScheduleResponse, dependencies=[dependencies.editors], summary="Обновить урок (PATCH)")
@limiter.limit("20/minute")
async def patch_lesson(
    request: Request, 
    lesson_id: int = Path(..., ge=1), 
    lesson: schemas.ScheduleUpdate = ..., 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Урок не найден")
        
        for field, value in lesson.model_dump(exclude_unset=True).items(): 
            setattr(row, field, value)
        
        row.approved_by = _make_user_info(user)
        
        await db.commit()
        await db.refresh(row)
        return schemas.ScheduleResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.delete("/{lesson_id}", response_model=schemas.ScheduleResponse, dependencies=[dependencies.admins], summary="Удалить урок")
@limiter.limit("20/minute")
async def delete_lesson(
    request: Request, 
    lesson_id: int = Path(..., ge=1), 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(ScheduleORM, lesson_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Урок не найден")
        
        row.approved_by = _make_user_info(user)
        
        await db.delete(row)
        await db.commit()
        return schemas.ScheduleResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc