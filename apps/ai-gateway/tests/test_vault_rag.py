from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.main import app, get_embedding_client, get_llm_client
from app.models import VaultChunk
from app.services.ollama_client import OllamaChatResult, OllamaUnavailableError
from app.token_repository import create_api_token


class FakeEmbeddingClient:
    def __init__(self, fail: bool = False, dimension: int = 3) -> None:
        self.fail = fail
        self.dimension = dimension

    async def embed_text(self, text: str):
        if self.fail:
            raise OllamaUnavailableError("unavailable")
        lowered = text.lower()
        if "couchdb" in lowered:
            vector = [1.0, 0.0, 0.0]
        elif "redis" in lowered:
            vector = [0.0, 1.0, 0.0]
        else:
            vector = [0.0, 0.0, 1.0]
        if self.dimension != 3:
            vector = vector + [0.0] * max(0, self.dimension - 3)
            vector = vector[: self.dimension]
        return type("EmbeddingResult", (), {"embedding": vector})()


class FakeVaultLlmClient:
    def __init__(self) -> None:
        self.last_system_prompt = ""
        self.last_user_prompt = ""

    async def vault_ask(
        self,
        *,
        model: str,
        question_chars: int,
        context_chars: int,
        system_prompt: str,
        user_prompt: str,
    ) -> OllamaChatResult:
        del question_chars, context_chars
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return OllamaChatResult(model=model, content="## Reponse\n\nD'apres les notes disponibles, CouchDB reste chiffre cote plugin.\n\n## Sources utilisees\n\n- [[Projects/RAG.md]]")


def create_token(scopes: list[str], user_id: str | None = None) -> str:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="vault-token", scopes=scopes, user_id=user_id)
        return created_token.plain_token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def index_payload(content: str = "# RAG\n\nDecision CouchDB: ne pas lire CouchDB cote backend.") -> dict[str, object]:
    return {
        "vault_id": "default",
        "path": "Projects/RAG.md",
        "title": "RAG",
        "content": content,
        "modified_at": "2026-05-25T12:00:00Z",
        "tags": ["infra"],
        "frontmatter": {"type": "note"},
        "metadata": {},
    }


def test_index_note_requires_vault_index_scope(client: TestClient) -> None:
    token = create_token(["vault:search"])
    response = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert response.status_code == 403


def test_index_note_creates_chunks_and_skips_identical_content(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    token = create_token(["vault:index"])
    response = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "indexed"
    assert response.json()["chunks_indexed"] >= 1

    second = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert second.status_code == 200
    assert second.json()["status"] == "skipped"
    with get_session_factory()() as session:
        chunk = session.query(VaultChunk).first()
        assert chunk is not None
        assert chunk.embedding.startswith("[")
        assert not hasattr(chunk, "embedding_json")


def test_index_note_rejects_embedding_dimension_mismatch(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient(dimension=2)
    token = create_token(["vault:index"])
    response = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert response.status_code == 503
    assert response.json()["detail"] == "Embedding dimension mismatch: expected 3, got 2. Check RAG_EMBEDDING_MODEL/RAG_EMBEDDING_DIMENSION."


def test_index_note_modified_replaces_old_chunks(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    token = create_token(["vault:index"])
    first = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload("# A\n\nCouchDB initial."))
    assert first.status_code == 200
    document_id = first.json()["document_id"]

    second = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload("# B\n\nCouchDB modifie."))
    assert second.status_code == 200
    assert second.json()["status"] == "indexed"
    with get_session_factory()() as session:
        chunks = session.query(VaultChunk).filter(VaultChunk.document_id == document_id).all()
        assert chunks
        assert all("modifie" in chunk.content for chunk in chunks)


def test_search_requires_vault_search_scope(client: TestClient) -> None:
    token = create_token(["vault:index"])
    response = client.post("/v1/vault/search", headers=bearer(token), json={"vault_id": "default", "query": "CouchDB"})
    assert response.status_code == 403


def test_search_returns_mocked_results(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="user-a")
    search_token = create_token(["vault:search"], user_id="user-a")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload())

    response = client.post("/v1/vault/search", headers=bearer(search_token), json={"vault_id": "default", "query": "CouchDB", "top_k": 8})
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"]
    assert payload["results"][0]["path"] == "Projects/RAG.md"
    assert len(payload["results"][0]["snippet"]) <= 320


def test_ask_requires_vault_ask_scope(client: TestClient) -> None:
    token = create_token(["vault:search"])
    response = client.post("/v1/vault/ask", headers=bearer(token), json={"vault_id": "default", "question": "CouchDB ?"})
    assert response.status_code == 403


def test_ask_builds_rag_prompt_with_sources_and_context_limit(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RAG_MAX_CONTEXT_CHARS", "180")
    from app.config import get_settings

    get_settings.cache_clear()
    fake_llm = FakeVaultLlmClient()
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    index_token = create_token(["vault:index"], user_id="user-b")
    ask_token = create_token(["vault:ask"], user_id="user-b")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload("# RAG\n\nCouchDB " + "details " * 200))

    response = client.post("/v1/vault/ask", headers=bearer(ask_token), json={"vault_id": "default", "question": "Quelle decision CouchDB ?", "model": "qwen2.5:14b"})
    assert response.status_code == 200
    assert response.json()["sources"]
    assert "Projects/RAG.md" in fake_llm.last_user_prompt
    assert len(fake_llm.last_user_prompt) < 600
    get_settings.cache_clear()


def test_ask_with_no_relevant_sources_returns_insufficient_information(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RAG_MIN_SCORE", "0.99")
    from app.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="user-c")
    ask_token = create_token(["vault:ask"], user_id="user-c")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload())

    response = client.post("/v1/vault/ask", headers=bearer(ask_token), json={"vault_id": "default", "question": "Redis ?", "model": "qwen2.5:14b"})
    assert response.status_code == 200
    assert "pas assez d'informations" in response.json()["answer_markdown"]
    get_settings.cache_clear()


def test_stats_and_delete_index(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="user-d")
    search_token = create_token(["vault:search"], user_id="user-d")
    admin_token = create_token(["vault:admin"], user_id="user-d")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload())

    stats = client.get("/v1/vault/stats?vault_id=default", headers=bearer(search_token))
    assert stats.status_code == 200
    assert stats.json()["documents"] == 1
    assert stats.json()["chunks"] >= 1

    forbidden = client.delete("/v1/vault/index?vault_id=default", headers=bearer(search_token))
    assert forbidden.status_code == 403

    deleted = client.delete("/v1/vault/index?vault_id=default", headers=bearer(admin_token))
    assert deleted.status_code == 200
    assert deleted.json()["deleted_documents"] == 1


def test_embedding_unavailable_returns_controlled_error(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient(fail=True)
    token = create_token(["vault:index"])
    response = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert response.status_code == 503
    assert response.json() == {"detail": "The embedding backend is currently unavailable."}


def test_rag_disabled_returns_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RAG_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    token = create_token(["vault:index"])
    response = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload())
    assert response.status_code == 503
    assert response.json() == {"detail": "Vault RAG is disabled."}
    get_settings.cache_clear()


def test_vault_ask_rejects_forbidden_model(client: TestClient) -> None:
    token = create_token(["vault:ask"])
    response = client.post("/v1/vault/ask", headers=bearer(token), json={"vault_id": "default", "question": "CouchDB ?", "model": "forbidden:latest"})
    assert response.status_code == 403


def test_default_rag_vector_backend_is_pgvector(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.delenv("RAG_VECTOR_BACKEND", raising=False)
    get_settings.cache_clear()
    assert get_settings().rag_vector_backend == "pgvector"
    assert get_settings().rag_embedding_dimension == 768
    get_settings.cache_clear()
