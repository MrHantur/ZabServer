# app/routers/public.py
from typing import Optional
from fastapi import APIRouter, Depends, Request, Path, Query, HTTPException
from sqlalchemy import select
from app import schemas, dependencies
from app.database import SessionLocal
from app.models import ScheduleORM, OlympiadORM
from app.core import limiter

router = APIRouter(prefix="/public", tags=["Публичные"])

@router.get("/schedule/{weekday}", response_model=schemas.ScheduleResponse, summary="Расписание")
@limiter.limit("120/minute")
async def public_get_schedule_by_day(request: Request, weekday: int = Path(..., ge=0, le=6), class_name: Optional[str] = Query(None), db: SessionLocal = Depends(dependencies.get_db)):
    try:
        query = select(ScheduleORM).where(ScheduleORM.weekday == weekday)
        if class_name:
            query = query.where(ScheduleORM.class_name == class_name)
        query = query.order_by(ScheduleORM.class_name, ScheduleORM.lesson_num)
        rows = await db.execute(query)
        return schemas.ScheduleResponse(success=True, data=rows.scalars().all())
    except Exception as exc:
        raise dependencies._db_error(exc) from exc

@router.get("/olympiads", response_model=schemas.OlympiadResponse, summary="Список олимпиад")
@limiter.limit("120/minute")
async def public_get_olympiads(request: Request, date_start: Optional[str] = Query(None), subject: Optional[str] = Query(None), level: Optional[int] = Query(None), classes: Optional[str] = Query(None), db: SessionLocal = Depends(dependencies.get_db)):
    try:
        query = select(OlympiadORM).order_by(OlympiadORM.date_start)
        if date_start: query = query.where(OlympiadORM.date_start == date_start)
        if subject: query = query.where(OlympiadORM.subject == subject)
        if level is not None: query = query.where(OlympiadORM.level == level)
        if classes: query = query.where(OlympiadORM.classes == classes)
        rows = await db.execute(query)
        return schemas.OlympiadResponse(success=True, data=rows.scalars().all())
    except Exception as exc:
        raise dependencies._db_error(exc) from exc

@router.get("/olympiads/{olympiad_id}", response_model=schemas.OlympiadResponse, summary="Одна олимпиада")
@limiter.limit("120/minute")
async def public_get_olympiad(request: Request, olympiad_id: int = Path(..., ge=1), db: SessionLocal = Depends(dependencies.get_db)):
    row = await db.get(OlympiadORM, olympiad_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Олимпиада не найдена")
    return schemas.OlympiadResponse(success=True, data=[row])