import uuid
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.schemas.interfaces import ModelRegistry, UserCtx


def test_health_and_no_openapi_leak():
    c = TestClient(create_app())
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/openapi.json").status_code == 404


def test_userctx_frozen_and_strict():
    u = UserCtx(id=uuid.uuid4(), tier="free", email_verified=True)
    import pytest
    with pytest.raises(Exception):
        u.tier = "paid"
    with pytest.raises(Exception):
        UserCtx(id=uuid.uuid4(), tier="gold", email_verified=True)


def test_registry_protocol_is_runtime_checkable():
    class Fake:
        def list_models(self): return []
        def model_card(self, e): raise KeyError(e)
        def time_estimate_seconds(self, e, n, c): return 1.0
        def predict(self, e, s, n): raise NotImplementedError
    assert isinstance(Fake(), ModelRegistry)
