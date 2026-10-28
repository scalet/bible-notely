from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api import router
from backend.app.config import settings
from backend.app.db import SessionLocal, ensure_schema
from backend.app.logging_setup import setup_logging
from backend.app.runtime import cancel_event
from backend.app.services.pipeline import PipelineBusy, pipeline_lock, recover_interrupted

setup_logging()
ensure_schema()

app = FastAPI(title="bible-notely", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _recover_stale() -> None:
    try:
        with pipeline_lock():
            db = SessionLocal()
            try:
                recover_interrupted(db)
            finally:
                db.close()
    except PipelineBusy:
        pass


@app.on_event("shutdown")
def _stop_work() -> None:
    cancel_event.set()


app.include_router(router)

dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=dist, html=True), name="ui")


@app.get("/api")
def root() -> dict:
    return {"name": "bible-notely"}
