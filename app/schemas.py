# app/schemas.py
import json as _json
from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field, model_validator, field_validator
from .models import ProposalORM

_NAME = Field(..., min_length=1, max_length=200)
_SHORT = Field(..., min_length=1, max_length=100)
_OPT30 = Field(None, max_length=30)
_OPT100 = Field(None, max_length=100)
_OPT500 = Field(None, max_length=500)
_TIME = Field(None, pattern=r"^\d{2}:\d{2}$")

class UserInfo(BaseModel):
    username: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field("viewer", pattern=r"^(viewer|contributor|editor|admin)$")
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)

class UserRead(BaseModel):
    id: int
    username: str
    role: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    karma: int = 0
    model_config = {"from_attributes": True}

class UserUpdate(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)

class OlympiadBase(BaseModel):
    name: str = _NAME
    description: Optional[str] = _OPT500
    subject: str = _SHORT
    date_start: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_end: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: Optional[str] = _TIME
    classes: Optional[str] = Field(None, pattern=r"^(?:[1-9]|10|11)-(?:[1-9]|10|11)$")
    stage: Optional[str] = _OPT30
    level: Optional[int] = Field(default=1, ge=1, le=3)
    link: Optional[str] = _OPT500
    created_by: Optional[Dict[str, str]] = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_dates(self):
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end не может быть раньше date_start")
        return self

class OlympiadRead(OlympiadBase):
    id: int
    created_by: UserInfo
    approved_by: Optional[UserInfo] = None
    proposal_id: Optional[int] = None
    model_config = {"from_attributes": True}
    
    @field_validator('created_by', 'approved_by', mode='before')
    @classmethod
    def parse_json_user(cls, v):
        """Парсит JSON-строку из БД в dict для валидации UserInfo"""
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except (_json.JSONDecodeError, TypeError):
                return v  # вернём как есть, пусть Pydantic выбросит свою ошибку
        return v

class OlympiadUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    subject: Optional[str] = Field(None, min_length=1, max_length=100)
    date_start: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_end: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    classes: Optional[str] = Field(None, min_length=1, max_length=100)
    level: Optional[int] = Field(None, ge=1, le=3)
    link: Optional[str] = Field(None, max_length=500)
    created_by: Optional[Dict[str, str]] = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_dates(self):
        if self.date_end is not None and self.date_end < self.date_start:
            raise ValueError("date_end не может быть раньше date_start")
        return self

class OlympiadResponse(BaseModel):
    success: bool
    data: List[OlympiadRead]
    error: Optional[str] = None

class ScheduleBase(BaseModel):
    class_name: str = Field(..., min_length=1, max_length=30)
    weekday: int = Field(..., ge=0, le=6)
    lesson_num: int = Field(..., ge=1, le=20)
    subject: str = _SHORT
    teacher: Optional[str] = _OPT100
    room: Optional[str] = _OPT30
    time_start: Optional[str] = _TIME
    time_end: Optional[str] = _TIME
    status: str = Field(default="active", pattern=r"^(active|cancelled)$")

class ScheduleRead(ScheduleBase):
    id: int
    created_by: UserInfo
    approved_by: Optional[UserInfo] = None
    proposal_id: Optional[int] = None
    model_config = {"from_attributes": True}
    
    @field_validator('created_by', 'approved_by', mode='before')
    @classmethod
    def parse_json_user(cls, v):
        """Парсит JSON-строку из БД в dict для валидации UserInfo"""
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except (_json.JSONDecodeError, TypeError):
                return v
        return v

class ScheduleUpdate(BaseModel):
    class_name: Optional[str] = Field(None, min_length=1, max_length=30)
    weekday: Optional[int] = Field(None, ge=0, le=6)
    lesson_num: Optional[int] = Field(None, ge=1, le=20)
    subject: Optional[str] = Field(None, min_length=1, max_length=100)
    teacher: Optional[str] = Field(None, max_length=100)
    room: Optional[str] = Field(None, max_length=30)
    time_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    time_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    status: Optional[str] = Field(None, pattern=r"^(active|cancelled)$")

class ScheduleResponse(BaseModel):
    success: bool
    data: List[ScheduleRead]
    error: Optional[str] = None

class ProposalCreate(BaseModel):
    entity_type: str = Field(..., pattern=r"^(olympiad|schedule)$")
    entity_id: Optional[int] = None
    action: str = Field(..., pattern=r"^(create|update|delete)$")
    payload: dict

class ProposalReview(BaseModel):
    decision: str = Field(..., pattern=r"^(approved|rejected)$")
    review_note: Optional[str] = Field(None, max_length=500)

class ProposalRead(BaseModel):
    id: int
    author: str
    entity_type: str
    entity_id: Optional[int]
    action: str
    payload: dict
    status: str
    created_at: str
    reviewed_by: Optional[str] = None
    review_note: Optional[str] = None

    @classmethod
    def from_orm(cls, row: ProposalORM) -> "ProposalRead":
        return cls(
            id=row.id, author=row.author, entity_type=row.entity_type,
            entity_id=row.entity_id, action=row.action,
            payload=_json.loads(row.payload), status=row.status,
            created_at=row.created_at, reviewed_by=row.reviewed_by,
            review_note=row.review_note,
        )

class ProposalResponse(BaseModel):
    success: bool
    data: List[ProposalRead]
    error: Optional[str] = None