from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.main import app, get_embedding_client, get_llm_client
from app.models import VaultChunk, VaultDocument
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


def test_reindex_same_path_removes_old_chunk_content(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    token = create_token(["vault:index"], user_id="replace-user")
    first = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload("# RAG\n\nALPHA-RAG-001 old content."))
    assert first.status_code == 200

    second = client.post("/v1/vault/index-note", headers=bearer(token), json=index_payload("# RAG\n\nBETA-RAG-002 new content."))
    assert second.status_code == 200

    with get_session_factory()() as session:
        contents = [chunk.content for chunk in session.query(VaultChunk).all()]
        assert any("BETA-RAG-002" in content for content in contents)
        assert not any("ALPHA-RAG-001" in content for content in contents)


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
    assert payload["results"][0]["vector_score"] is not None
    assert payload["results"][0]["keyword_bonus"] > 0
    assert "couchdb" in payload["results"][0]["matched_terms"]


def test_search_exact_keywords_boost_relevant_note(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="keyword-user")
    search_token = create_token(["vault:search"], user_id="keyword-user")
    client.post(
        "/v1/vault/index-note",
        headers=bearer(index_token),
        json=index_payload("# Test RAG CouchDB\n\nCouchDB LiveSync Nginx Proxy Manager E2EE obsidian_livesync."),
    )

    response = client.post(
        "/v1/vault/search",
        headers=bearer(search_token),
        json={"vault_id": "default", "query": "CouchDB LiveSync HTTPS obsidian_livesync", "top_k": 8},
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["path"] == "Projects/RAG.md"
    assert result["keyword_bonus"] > 0
    assert {"couchdb", "livesync", "obsidian_livesync"}.issubset(set(result["matched_terms"]))


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


def test_ask_debug_returns_safe_metadata_without_note_content(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    app.dependency_overrides[get_llm_client] = lambda: FakeVaultLlmClient()
    index_token = create_token(["vault:index"], user_id="debug-user")
    ask_token = create_token(["vault:ask"], user_id="debug-user")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload("# RAG\n\nSecret-ish CouchDB operational details."))

    response = client.post("/v1/vault/ask", headers=bearer(ask_token), json={"vault_id": "default", "question": "CouchDB ?", "model": "qwen2.5:14b", "debug": True})
    assert response.status_code == 200
    debug_info = response.json()["debug_info"]
    assert debug_info["selected_sources_count"] >= 1
    assert debug_info["selected_paths"] == ["Projects/RAG.md"]
    assert debug_info["top_vector_scores"]
    assert debug_info["top_keyword_bonuses"]
    assert debug_info["matched_terms_by_path"]["Projects/RAG.md"]
    assert "Secret-ish" not in str(debug_info)


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


def test_delete_document_requires_vault_index_and_removes_only_path(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="delete-user")
    search_token = create_token(["vault:search"], user_id="delete-user")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload())

    forbidden = client.delete("/v1/vault/document?vault_id=default&path=Projects%2FRAG.md", headers=bearer(search_token))
    assert forbidden.status_code == 403

    deleted = client.delete("/v1/vault/document?vault_id=default&path=Projects%2FRAG.md", headers=bearer(index_token))
    assert deleted.status_code == 200
    assert deleted.json()["deleted_documents"] == 1
    assert deleted.json()["document_deleted"] is True
    assert deleted.json()["path"] == "Projects/RAG.md"
    assert deleted.json()["deleted_chunks"] >= 1

    stats = client.get("/v1/vault/stats?vault_id=default", headers=bearer(search_token))
    assert stats.status_code == 200
    assert stats.json()["documents"] == 0


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


def test_workspace_id_shares_rag_index_across_tokens(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="workspace-user-a")
    search_token = create_token(["vault:search"], user_id="workspace-user-b")
    payload = {**index_payload(), "workspace_id": "shared-workspace"}
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=payload)

    search = client.post("/v1/vault/search", headers=bearer(search_token), json={"vault_id": "default", "workspace_id": "shared-workspace", "query": "CouchDB"})
    assert search.status_code == 200
    assert search.json()["results"][0]["path"] == "Projects/RAG.md"


def test_workspace_id_falls_back_to_user_id(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    index_token = create_token(["vault:index"], user_id="fallback-user-a")
    search_token = create_token(["vault:search"], user_id="fallback-user-b")
    client.post("/v1/vault/index-note", headers=bearer(index_token), json=index_payload())

    search = client.post("/v1/vault/search", headers=bearer(search_token), json={"vault_id": "default", "query": "CouchDB"})
    assert search.status_code == 200
    assert search.json()["results"] == []


def test_delete_index_without_all_users_keeps_other_workspace(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    token_a = create_token(["vault:index", "vault:admin", "vault:search"], user_id="purge-a")
    token_b = create_token(["vault:index", "vault:search"], user_id="purge-b")
    client.post("/v1/vault/index-note", headers=bearer(token_a), json={**index_payload(), "workspace_id": "workspace-a"})
    client.post("/v1/vault/index-note", headers=bearer(token_b), json={**index_payload(), "workspace_id": "workspace-b", "path": "Projects/Other.md"})

    deleted = client.delete("/v1/vault/index?vault_id=default&workspace_id=workspace-a", headers=bearer(token_a))
    assert deleted.status_code == 200
    assert deleted.json()["deleted_documents"] == 1

    stats_b = client.get("/v1/vault/stats?vault_id=default&workspace_id=workspace-b", headers=bearer(token_b))
    assert stats_b.status_code == 200
    assert stats_b.json()["documents"] == 1


def test_delete_index_all_users_removes_all_workspaces_for_vault_only(client: TestClient) -> None:
    app.dependency_overrides[get_embedding_client] = lambda: FakeEmbeddingClient()
    admin_token = create_token(["vault:index", "vault:admin", "vault:search"], user_id="all-admin")
    other_token = create_token(["vault:index", "vault:search"], user_id="all-other")
    client.post("/v1/vault/index-note", headers=bearer(admin_token), json={**index_payload(), "workspace_id": "workspace-a"})
    client.post("/v1/vault/index-note", headers=bearer(other_token), json={**index_payload(), "workspace_id": "workspace-b", "path": "Projects/Other.md"})
    client.post("/v1/vault/index-note", headers=bearer(other_token), json={**index_payload(), "vault_id": "other-vault", "workspace_id": "workspace-b", "path": "Projects/OtherVault.md"})

    deleted = client.delete("/v1/vault/index?vault_id=default&all_users=true", headers=bearer(admin_token))
    assert deleted.status_code == 200
    assert deleted.json()["all_users"] is True
    assert deleted.json()["deleted_documents"] == 2

    with get_session_factory()() as session:
        remaining = session.query(VaultDocument).filter(VaultDocument.vault_id == "other-vault").count()
        assert remaining == 1


def test_delete_index_all_users_requires_vault_admin(client: TestClient) -> None:
    token = create_token(["vault:index"], user_id="not-admin")
    response = client.delete("/v1/vault/index?vault_id=default&all_users=true", headers=bearer(token))
    assert response.status_code == 403


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
