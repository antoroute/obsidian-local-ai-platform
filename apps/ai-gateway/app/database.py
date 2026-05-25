from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, connect_args=connect_args)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from app.models import ApiToken, Job, VaultChunk, VaultDocument

    del ApiToken
    del Job
    del VaultChunk
    del VaultDocument
    engine = get_engine()
    ensure_pgvector_extension(engine)
    Base.metadata.create_all(bind=engine)
    ensure_job_metadata_column(engine)
    ensure_vault_workspace_columns(engine)
    ensure_pgvector_support(engine)


def ensure_job_metadata_column(engine: Engine) -> None:
    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "metadata_json" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN metadata_json TEXT"))


def ensure_vault_workspace_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "vault_documents" not in tables or "vault_chunks" not in tables:
        return
    document_columns = {column["name"] for column in inspector.get_columns("vault_documents")}
    chunk_columns = {column["name"] for column in inspector.get_columns("vault_chunks")}
    with engine.begin() as connection:
        if "workspace_id" not in document_columns:
            connection.execute(text("ALTER TABLE vault_documents ADD COLUMN workspace_id VARCHAR(100)"))
            connection.execute(text("UPDATE vault_documents SET workspace_id = user_id WHERE workspace_id IS NULL OR workspace_id = ''"))
        if "workspace_id" not in chunk_columns:
            connection.execute(text("ALTER TABLE vault_chunks ADD COLUMN workspace_id VARCHAR(100)"))
            connection.execute(text("UPDATE vault_chunks SET workspace_id = user_id WHERE workspace_id IS NULL OR workspace_id = ''"))
        if engine.dialect.name == "postgresql":
            connection.execute(text("ALTER TABLE vault_documents ALTER COLUMN workspace_id SET NOT NULL"))
            connection.execute(text("ALTER TABLE vault_chunks ALTER COLUMN workspace_id SET NOT NULL"))


def ensure_pgvector_extension(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def ensure_pgvector_support(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    settings = get_settings()
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(f"ALTER TABLE vault_chunks ADD COLUMN IF NOT EXISTS embedding vector({settings.rag_embedding_dimension})"))
        connection.execute(text(f"ALTER TABLE vault_chunks ALTER COLUMN embedding TYPE vector({settings.rag_embedding_dimension})"))
        connection.execute(text("DELETE FROM vault_chunks WHERE embedding IS NULL"))
        connection.execute(text("ALTER TABLE vault_chunks ALTER COLUMN embedding SET NOT NULL"))
        connection.execute(text("ALTER TABLE vault_chunks DROP COLUMN IF EXISTS embedding_json"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_documents_user_vault ON vault_documents (user_id, vault_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_chunks_user_vault ON vault_chunks (user_id, vault_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_documents_workspace_vault ON vault_documents (workspace_id, vault_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_chunks_workspace_vault ON vault_chunks (workspace_id, vault_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_chunks_document ON vault_chunks (document_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_documents_user_vault_path ON vault_documents (user_id, vault_id, path)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_documents_workspace_vault_path ON vault_documents (workspace_id, vault_id, path)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_vault_document_workspace_vault_path_idx ON vault_documents (workspace_id, vault_id, path)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_vault_chunks_path ON vault_chunks (path)"))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_vault_chunks_embedding_hnsw ON vault_chunks USING hnsw (embedding vector_cosine_ops)"))
    except Exception:
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX IF NOT EXISTS idx_vault_chunks_embedding_ivfflat ON vault_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"))
