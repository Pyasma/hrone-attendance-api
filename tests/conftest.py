import os

import pytest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client():
    main.client.drop_database("attendance_test")
    main.indexes_ready.clear()
    with TestClient(main.app) as c:
        assert main.indexes_ready.wait(10)
        yield c
