# app/utils.py
import jwt
from datetime import datetime, timedelta, timezone
from bcrypt import checkpw, gensalt, hashpw
from fastapi import HTTPException
from .config import JWT_SECRET, JWT_ALGORITHM, ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL
from .schemas import TokenPair

def hash_password(password: str) -> str:
    return hashpw(password.encode("utf-8"), gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

def _make_token(sub: str, role: str, ttl_minutes: int, kind: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": sub, "role": role, "kind": kind,
        "iat": now, "exp": now + timedelta(minutes=ttl_minutes),
    }, JWT_SECRET, algorithm=JWT_ALGORITHM)

def _make_token_pair(username: str, role: str) -> TokenPair:
    return TokenPair(
        access_token=_make_token(username, role, ACCESS_TOKEN_TTL, "access"),
        refresh_token=_make_token(username, role, REFRESH_TOKEN_TTL, "refresh"),
    )

def _decode(token: str, expected_kind: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Токен истёк")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Неверный токен")
    
    if payload.get("kind") != expected_kind:
        raise HTTPException(status_code=401, detail="Неверный тип токена")
    return payload