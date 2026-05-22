from fastapi.testclient import TestClient
from sqlalchemy import update

from app.database import get_session_factory
from app.models import ApiToken
from app.token_repository import create_api_token


def create_bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_endpoint_returns_ok_without_token(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_endpoint_requires_token(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 401


def test_models_endpoint_rejects_invalid_token(client: TestClient) -> None:
    response = client.get("/v1/models", headers=create_bearer_header("obsai_live_invalid"))

    assert response.status_code == 401


def test_models_endpoint_accepts_valid_token_with_scope(client: TestClient) -> None:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="models-reader", scopes=["models:list"])

    response = client.get("/v1/models", headers=create_bearer_header(created_token.plain_token))

    assert response.status_code == 200
    assert response.json() == {"models": ["qwen2.5:14b", "mistral:7b"]}


def test_models_endpoint_rejects_missing_scope(client: TestClient) -> None:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="notes-only", scopes=["notes:summarize"])

    response = client.get("/v1/models", headers=create_bearer_header(created_token.plain_token))

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing required scope: models:list"}


def test_models_endpoint_rejects_revoked_token(client: TestClient) -> None:
    with get_session_factory()() as session:
        created_token = create_api_token(session, name="revoked-token", scopes=["models:list"])
        session.execute(
            update(ApiToken)
            .where(ApiToken.token_hash == created_token.token_hash)
            .values(revoked=True)
        )
        session.commit()

    response = client.get("/v1/models", headers=create_bearer_header(created_token.plain_token))

    assert response.status_code == 401
