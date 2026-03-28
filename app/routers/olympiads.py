# app/routers/olympiads.py
from fastapi import APIRouter, Depends, Request, Path, HTTPException
import json as _json
from app import schemas, dependencies
from app.database import SessionLocal
from app.models import OlympiadORM
from app.core import limiter

router = APIRouter(prefix="/olympiads", tags=["Олимпиады"])

def _make_user_info(user: dict) -> str:
    """Сериализует информацию о пользователе в JSON-строку"""
    return _json.dumps({
        "username": user["sub"],
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", "")
    }, ensure_ascii=False)

@router.post("", response_model=schemas.OlympiadResponse, status_code=201, dependencies=[dependencies.editors], summary="Создать олимпиаду")
@limiter.limit("20/minute")
async def create_olympiad(
    request: Request, 
    olympiad: schemas.OlympiadBase, 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        data = olympiad.model_dump()
        data['created_by'] = _make_user_info(user)
        data['approved_by'] = _make_user_info(user)
        
        new_row = OlympiadORM(**data)
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        return schemas.OlympiadResponse(success=True, data=[new_row])
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.put("/{olympiad_id}", response_model=schemas.OlympiadResponse, dependencies=[dependencies.editors], summary="Обновить олимпиаду (PUT)")
@limiter.limit("20/minute")
async def update_olympiad(
    request: Request, 
    olympiad_id: int = Path(..., ge=1), 
    olympiad: schemas.OlympiadBase = ..., 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        
        for field, value in olympiad.model_dump().items(): 
            setattr(row, field, value)
        
        row.approved_by = _make_user_info(user)
        
        await db.commit()
        await db.refresh(row)
        return schemas.OlympiadResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.patch("/{olympiad_id}", response_model=schemas.OlympiadResponse, dependencies=[dependencies.editors], summary="Обновить олимпиаду (PATCH)")
@limiter.limit("20/minute")
async def patch_olympiad(
    request: Request, 
    olympiad_id: int = Path(..., ge=1), 
    olympiad: schemas.OlympiadUpdate = ..., 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        
        for field, value in olympiad.model_dump(exclude_unset=True).items(): 
            setattr(row, field, value)
        
        row.approved_by = _make_user_info(user)
        
        await db.commit()
        await db.refresh(row)
        return schemas.OlympiadResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.delete("/{olympiad_id}", response_model=schemas.OlympiadResponse, dependencies=[dependencies.editors], summary="Удалить олимпиаду")
@limiter.limit("20/minute")
async def delete_olympiad(
    request: Request, 
    olympiad_id: int = Path(..., ge=1), 
    user: dict = Depends(dependencies.current_user),
    db: SessionLocal = Depends(dependencies.get_db)
):
    try:
        row = await db.get(OlympiadORM, olympiad_id)
        if row is None: 
            raise HTTPException(status_code=404, detail="Олимпиада не найдена")
        
        # Можно сохранить кто удалил перед удалением (опционально)
        row.approved_by = _make_user_info(user)
        
        await db.delete(row)
        await db.commit()
        return schemas.OlympiadResponse(success=True, data=[row])
    except HTTPException: 
        raise
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc