# app/core.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI, Request
from .dependencies import get_limiter_key

limiter = Limiter(key_func=get_limiter_key)

def register_exception_handlers(app: FastAPI):
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)