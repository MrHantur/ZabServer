# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import engine, Base
from app.core import limiter, register_exception_handlers
from app.routers import auth, users, public, olympiads, schedule, proposals

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title="Zab API", version="1.3.0", lifespan=lifespan)
app.state.limiter = limiter
register_exception_handlers(app)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(public.router)
app.include_router(olympiads.router)
app.include_router(schedule.router)
app.include_router(proposals.router)