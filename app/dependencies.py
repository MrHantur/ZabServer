# app/dependencies.py
from typing import AsyncGenerator, Optional
from functools import lru_cache
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import SessionLocal
from .utils import _decode
from .config import JWT_SECRET, JWT_ALGORITHM
import jwt

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

async def current_user(token: Optional[str] = Depends(oauth2)) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Необходима авторизация")
    return _decode(token, "access")

async def optional_user(token: Optional[str] = Depends(oauth2)) -> Optional[dict]:
    if not token:
        return None
    try:
        return _decode(token, "access")
    except HTTPException:
        return None

@lru_cache(maxsize=16)
def require_role(*roles: str):
    async def _check(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return _check

any_user = Depends(current_user)
editors = Depends(require_role("editor", "admin"))
admins = Depends(require_role("admin"))
contributors = Depends(require_role("contributor", "editor", "admin"))

def _db_error(exc: Exception) -> HTTPException:
    import logging
    logger = logging.getLogger(__name__)
    logger.error("DB error: %s", exc, exc_info=True)
    return HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

def get_limiter_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
            return f"user:{payload.get('sub', 'anon')}"
        except Exception:
            pass
    return f"ip:{request.client.host}"