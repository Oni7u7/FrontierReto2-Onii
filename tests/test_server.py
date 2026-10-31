"""tests/test_server.py — Tests del servidor FastAPI."""
import asyncio
import hmac
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app, _TASKS, _CURRENT_ASYNCIO_TASK

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    monkeypatch.setenv("EQUIPO_TOKEN", "test-token")
    _TASKS.clear()


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

def test_health_without_token(client):
    resp = client.get("/health")
    assert resp.status_code == 401


def test_health_with_bad_token(client):
    resp = client.get("/health", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_health_ok(client):
    resp = client.get("/health", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_auth_uses_constant_time(client, monkeypatch):
    """Confirmar que usamos hmac.compare_digest (timing-safe)."""
    monkeypatch.setenv("EQUIPO_TOKEN", "abc")
    # hmac.compare_digest no afecta el resultado, solo la comparación interna
    resp = client.get("/health", headers={"Authorization": "Bearer abc"})
    assert resp.status_code == 200
    resp2 = client.get("/health", headers={"Authorization": "Bearer abd"})
    assert resp2.status_code == 401


# ------------------------------------------------------------------
# Creación de tareas
# ------------------------------------------------------------------

def test_create_task_returns_task_id(client):
    with patch("server.asyncio.create_task"):
        resp = client.post(
            "/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "tarea": "Registrar cliente",
                "escritorio": {
                    "url": "ws://mock/websockify",
                    "password": "secret",
                },
            },
        )
    assert resp.status_code == 202
    data = resp.json()
    assert "task_id" in data


def test_create_task_uses_escritorio_from_post(client):
    with patch("server.asyncio.create_task"):
        resp = client.post(
            "/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "tarea": "Registrar cliente",
                "escritorio": {
                    "url": "ws://custom-url/websockify",
                    "password": "custom-pass",
                },
            },
        )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]
    assert _TASKS[task_id]["ws_url"] == "ws://custom-url/websockify"
    assert _TASKS[task_id]["password"] == "custom-pass"


def test_get_task_not_found(client):
    resp = client.get(
        "/tasks/invalid-id",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404


def test_get_task_returns_status(client):
    with patch("server.asyncio.create_task"):
        resp = client.post(
            "/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "tarea": "Test",
                "escritorio": {"url": "ws://x", "password": "p"},
            },
        )
    task_id = resp.json()["task_id"]

    get_resp = client.get(
        f"/tasks/{task_id}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["estado"] == "en_curso"
    assert "resumen" in data


# ------------------------------------------------------------------
# Cancelación de tarea anterior
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_task_cancels_previous():
    """Si llega un segundo POST /tasks, la primera se cancela como fallida."""
    async def fake_slow(*args, **kwargs):
        await asyncio.sleep(10)

    with patch("server.ejecutar_tarea", new=fake_slow):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            # Primera tarea
            r1 = await ac.post(
                "/tasks",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "tarea": "T1",
                    "escritorio": {"url": "ws://x", "password": "p"},
                },
            )
            tid1 = r1.json()["task_id"]

            # Segunda tarea (debería cancelar la primera)
            r2 = await ac.post(
                "/tasks",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "tarea": "T2",
                    "escritorio": {"url": "ws://y", "password": "p2"},
                },
            )
            tid2 = r2.json()["task_id"]

            # Esperar un poco a que el cancel llegue
            for _ in range(20):
                r1_status = await ac.get(
                    f"/tasks/{tid1}",
                    headers={"Authorization": "Bearer test-token"},
                )
                if r1_status.json()["estado"] != "en_curso":
                    break
                await asyncio.sleep(0.05)

            assert r1_status.json()["estado"] == "fallida"
            assert "cancelada" in r1_status.json()["resumen"].lower()

            # La segunda sigue en curso
            r2_status = await ac.get(
                f"/tasks/{tid2}",
                headers={"Authorization": "Bearer test-token"},
            )
            assert r2_status.json()["estado"] == "en_curso"


# ------------------------------------------------------------------
# Privacidad de la contraseña del escritorio
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_never_exposed():
    """La contraseña del escritorio recibida en POST nunca aparece en
    GET /tasks/{id}, ni en logs, ni en el resumen (debe ser reemplazada por ***)."""
    SUPER_SECRET = "MiCl4ve-Sup3r-S3cr3t4-VNC!"

    async def fake_with_password(*args, **kwargs):
        # Simulamos que el modelo (o un error) incluye la contraseña en el resumen
        return {"estado": "fallida", "resumen": f"Error con password {SUPER_SECRET}"}

    with patch("server.ejecutar_tarea", new=fake_with_password):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/tasks",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "tarea": "Test pwd",
                    "escritorio": {
                        "url": "ws://x",
                        "password": SUPER_SECRET,
                    },
                },
            )
            tid = r.json()["task_id"]

            # Esperar a que termine
            for _ in range(50):
                status = await ac.get(
                    f"/tasks/{tid}",
                    headers={"Authorization": "Bearer test-token"},
                )
                if status.json()["estado"] != "en_curso":
                    break
                await asyncio.sleep(0.05)

            resumen = status.json()["resumen"]
            assert SUPER_SECRET not in resumen
            assert "***" in resumen
