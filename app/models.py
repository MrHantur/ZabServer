# app/models.py
from sqlalchemy import Column, Integer, String
from .database import Base

class UserORM(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="viewer")
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    karma = Column(Integer, nullable=False, default=0)

class OlympiadORM(Base):
    __tablename__ = "olympiads"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    subject = Column(String, nullable=False)
    date_start = Column(String, nullable=False)
    date_end = Column(String, nullable=True)
    time = Column(String, nullable=True)
    classes = Column(String, nullable=True)
    stage = Column(String, nullable=True)
    level = Column(Integer, nullable=True)
    link = Column(String, nullable=True)
    
    # Поля аудита (хранят JSON-строку с username, first_name, last_name)
    created_by = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    proposal_id = Column(Integer, nullable=True)

class ScheduleORM(Base):
    __tablename__ = "schedule"
    id = Column(Integer, primary_key=True, index=True)
    class_name = Column(String, nullable=False, index=True)
    weekday = Column(Integer, nullable=False, index=True)
    lesson_num = Column(Integer, nullable=False)
    subject = Column(String, nullable=False)
    teacher = Column(String, nullable=True)
    room = Column(String, nullable=True)
    time_start = Column(String, nullable=True)
    time_end = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    
    # Поля аудита (хранят JSON-строку с username, first_name, last_name)
    created_by = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    proposal_id = Column(Integer, nullable=True)

class ProposalORM(Base):
    __tablename__ = "proposals"
    id = Column(Integer, primary_key=True, index=True)
    author = Column(String, nullable=False, index=True)  # username автора
    entity_type = Column(String, nullable=False)
    entity_id = Column(Integer, nullable=True)
    action = Column(String, nullable=False)
    payload = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(String, nullable=False)
    reviewed_by = Column(String, nullable=True)  # username ревьювера
    review_note = Column(String, nullable=True)