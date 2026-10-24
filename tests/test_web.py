from fastapi.testclient import TestClient

from gopro_sync.web import create_app


def make_client(config, db, service, nas) -> TestClient:
    return TestClient(create_app(config, db, service, nas))


def test_manual_and_its_pictures_are_served(config, db, service, nas):
    import re

    client = make_client(config, db, service, nas)
    manual = client.get("/static/manual.html")
    schema = client.get("/static/schema.svg")
    assert manual.status_code == 200 and schema.status_code == 200
    refs = set(re.findall(r'(?:src|data)="(/static/[^"]+)"', manual.text))
    refs |= {f"/static/{p}" for p in re.findall(r'\shref="(img/[^"]+)"', schema.text)}
    assert len(refs) >= 4
    for ref in refs:
        assert client.get(ref).status_code == 200, ref


def test_status_and_index(config, db, service, nas):
    client = make_client(config, db, service, nas)
    assert client.get("/").status_code == 200
    body = client.get("/api/status").json()
    assert body["busy"] is False and body["auth_required"] is False


def test_token_required_for_changes(config, db, service, nas):
    cfg = config.config.model_copy(deep=True)
    cfg.server.api_token = "abc"
    config.save(cfg)
    client = make_client(config, db, service, nas)
    assert client.delete("/api/transferred").status_code == 401
    assert client.delete("/api/transferred", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.delete("/api/transferred", headers={"Authorization": "Bearer abc"}).status_code == 200
    assert client.get("/api/transferred").status_code == 200


def test_transferred_reset_endpoints(config, db, service, nas):
    serial = db.list_cameras()[0].serial
    db.add_transferred(serial, "100GOPRO/A.MP4", 1, 10, "/nas/A.MP4")
    db.add_transferred(serial, "100GOPRO/B.MP4", 2, 10, "/nas/B.MP4")
    client = make_client(config, db, service, nas)
    items = client.get("/api/transferred").json()["items"]
    assert len(items) == 2
    assert client.delete(f"/api/transferred/{items[0]['id']}").status_code == 200
    assert client.get("/api/transferred").json()["total"] == 1
    assert client.delete("/api/transferred").json()["deleted"] == 1


def test_cameras_endpoint(config, db, service, nas):
    client = make_client(config, db, service, nas)
    data = client.get("/api/cameras").json()
    assert data["cameras"][0]["family"] == "HERO11"
    serial = data["cameras"][0]["serial"]
    assert client.patch(f"/api/cameras/{serial}", json={"enabled": False}).status_code == 200
    assert db.get_camera(serial).enabled is False
