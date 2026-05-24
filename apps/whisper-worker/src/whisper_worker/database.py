from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from whisper_worker.config import WorkerSettings


class Base(DeclarativeBase):
    pass


def create_engine_for_settings(settings: WorkerSettings) -> Engine:
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, connect_args=connect_args)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_job_metadata_column(engine: Engine) -> None:
    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "metadata_json" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN metadata_json TEXT"))
