from fastapi.testclient import TestClient

from clipforge.api.app import create_app


def api_client() -> TestClient:
    """A client that talks to the API like the web app: local host + X-ClipForge header."""
    return TestClient(create_app(), base_url="http://127.0.0.1", headers={"X-ClipForge": "1"})
