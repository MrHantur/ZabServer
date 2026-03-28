# app/routers/proposals.py
import json as _json
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Request, Path, Query, HTTPException
from sqlalchemy import select
from app import schemas, dependencies
from app.database import SessionLocal
from app.models import ProposalORM, UserORM, OlympiadORM, ScheduleORM
from app.core import limiter

router = APIRouter(prefix="/proposals", tags=["Предложения изменений"])

@router.post("", response_model=schemas.ProposalResponse, status_code=201, summary="Предложить изменение")
@limiter.limit("30/minute")
async def create_proposal(request: Request, body: schemas.ProposalCreate, user: dict = Depends(dependencies.require_role("contributor", "editor", "admin")), db: SessionLocal = Depends(dependencies.get_db)):
    if body.action in ("update", "delete") and body.entity_id is None:
        raise HTTPException(status_code=422, detail="entity_id обязателен для update/delete")
    if body.action == "create" and body.entity_id is not None:
        raise HTTPException(status_code=422, detail="entity_id должен быть None для create")
    now = datetime.now(timezone.utc).isoformat()
    new_proposal = ProposalORM(
        author=user["sub"], entity_type=body.entity_type, entity_id=body.entity_id,
        action=body.action, payload=_json.dumps(body.payload, ensure_ascii=False),
        status="pending", created_at=now,
    )
    try:
        db.add(new_proposal)
        await db.commit()
        await db.refresh(new_proposal)
        return schemas.ProposalResponse(success=True, data=[schemas.ProposalRead.from_orm(new_proposal)])
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc

@router.get("", response_model=schemas.ProposalResponse, summary="Список предложений")
@limiter.limit("60/minute")
async def list_proposals(request: Request, status_filter: Optional[str] = Query(None, alias="status"), entity_type: Optional[str] = Query(None), user: dict = Depends(dependencies.require_role("editor", "admin")), db: SessionLocal = Depends(dependencies.get_db)):
    try:
        query = select(ProposalORM).order_by(ProposalORM.created_at.desc())
        if status_filter: query = query.where(ProposalORM.status == status_filter)
        if entity_type: query = query.where(ProposalORM.entity_type == entity_type)
        rows = await db.execute(query)
        proposals = [schemas.ProposalRead.from_orm(r) for r in rows.scalars().all()]
        return schemas.ProposalResponse(success=True, data=proposals)
    except Exception as exc:
        raise dependencies._db_error(exc) from exc

@router.get("/my", response_model=schemas.ProposalResponse, summary="Мои предложения")
@limiter.limit("60/minute")
async def my_proposals(request: Request, user: dict = Depends(dependencies.current_user), db: SessionLocal = Depends(dependencies.get_db)):
    try:
        query = select(ProposalORM).where(ProposalORM.author == user["sub"]).order_by(ProposalORM.created_at.desc())
        rows = await db.execute(query)
        proposals = [schemas.ProposalRead.from_orm(r) for r in rows.scalars().all()]
        return schemas.ProposalResponse(success=True, data=proposals)
    except Exception as exc:
        raise dependencies._db_error(exc) from exc

@router.get("/{proposal_id}", response_model=schemas.ProposalResponse, summary="Одно предложение")
@limiter.limit("60/minute")
async def get_proposal(request: Request, proposal_id: int = Path(..., ge=1), user: dict = Depends(dependencies.current_user), db: SessionLocal = Depends(dependencies.get_db)):
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None: raise HTTPException(status_code=404, detail="Предложение не найдено")
    if user["role"] not in ("editor", "admin") and proposal.author != user["sub"]:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return schemas.ProposalResponse(success=True, data=[schemas.ProposalRead.from_orm(proposal)])

@router.post("/{proposal_id}/review", response_model=schemas.ProposalResponse, summary="Рецензирование")
@limiter.limit("30/minute")
async def review_proposal(request: Request, proposal_id: int = Path(..., ge=1), body: schemas.ProposalReview = ..., user: dict = Depends(dependencies.require_role("editor", "admin")), db: SessionLocal = Depends(dependencies.get_db)):
    proposal = await db.get(ProposalORM, proposal_id)
    if proposal is None: raise HTTPException(status_code=404, detail="Предложение не найдено")
    if proposal.status != "pending": raise HTTPException(status_code=409, detail="Предложение уже рассмотрено")
    
    proposal.status = body.decision
    proposal.reviewed_by = user["sub"]
    proposal.review_note = body.review_note
    
    karma_delta = +1 if body.decision == "approved" else -1
    author_row = await db.execute(select(UserORM).where(UserORM.username == proposal.author))
    author = author_row.scalars().first()
    if author is not None:
        author.karma += karma_delta
    
    if body.decision == "approved":
        payload = _json.loads(proposal.payload)
        orm_map = {"olympiad": OlympiadORM, "schedule": ScheduleORM}
        OrmClass = orm_map.get(proposal.entity_type)
        if OrmClass is None: 
            raise HTTPException(status_code=422, detail="Неизвестный тип сущности")
        
        # Сериализуем информацию о пользователях
        author_info = _json.dumps({
            "username": proposal.author,
            "first_name": "",  # Можно запросить из БД если нужно
            "last_name": ""
        }, ensure_ascii=False)
        
        reviewer_info = _json.dumps({
            "username": user["sub"],
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", "")
        }, ensure_ascii=False)
        
        try:
            if proposal.action == "create":
                payload['created_by'] = author_info
                payload['approved_by'] = reviewer_info
                payload['proposal_id'] = proposal.id
                db.add(OrmClass(**payload))
                
            elif proposal.action == "update":
                row = await db.get(OrmClass, proposal.entity_id)
                if row is None: 
                    raise HTTPException(status_code=404, detail="Исходная запись не найдена")
                for field, value in payload.items(): 
                    setattr(row, field, value)
                row.approved_by = reviewer_info
                row.proposal_id = proposal.id
                
            elif proposal.action == "delete":
                row = await db.get(OrmClass, proposal.entity_id)
                if row is None: 
                    raise HTTPException(status_code=404, detail="Исходная запись не найдена")
                row.approved_by = reviewer_info  # Логируем кто удалил перед удалением
                await db.delete(row)
        except HTTPException: raise
        except Exception as exc:
            await db.rollback()
            raise dependencies._db_error(exc) from exc
    
    try:
        await db.commit()
        await db.refresh(proposal)
        return schemas.ProposalResponse(success=True, data=[schemas.ProposalRead.from_orm(proposal)])
    except Exception as exc:
        await db.rollback()
        raise dependencies._db_error(exc) from exc