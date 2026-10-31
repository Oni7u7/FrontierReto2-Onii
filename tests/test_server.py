"""tests/test_server.py — Tests del servidor FastAPI."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app, _TASKS, _TASK_QUEUE


@pytest.fixture(autouse=True)
def clear_state(monkeypatch):
    monkeypatch.setenv("EQUIPO_TOKEN", "test-token")
    _TASKS.clear()
    # Vaciar cola
    while not _TASK_QUEUE.empty():
        try:
            _TASK_QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            break


@pytest.fixture
def client():
    return TestClient(app)


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


def test_create_task_returns_task_id(client):
    with patch("server._maybe_start_next", new=AsyncMock()):
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
    with patch("server._maybe_start_next", new=AsyncMock()):
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
    with patch("server._maybe_start_next", new=AsyncMock()):
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
    assert data["estado"] == "encolada"
    assert "resumen" in data


@pytest.mark.asyncio
async def test_task_runs_in_background():
    """POST /tasks encola; el worker la ejecuta en segundo plano."""
    async def fake_ejecutar(*args, **kwargs):
        return {"estado": "terminada", "resumen": "Hecho"}

    with patch("server.ejecutar_tarea", new=fake_ejecutar):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/tasks",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "tarea": "Test bg",
                    "escritorio": {"url": "ws://x", "password": "p"},
                },
            )
            assert resp.status_code == 202
            task_id = resp.json()["task_id"]

            # Esperar a que el worker corra
            for _ in range(50):
                get_resp = await ac.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": "Bearer test-token"},
                )
                if get_resp.json()["estado"] != "encolada":
                    break
                await asyncio.sleep(0.05)

            assert get_resp.json()["estado"] == "terminada"
            assert get_resp.json()["resumen"] == "Hecho"
