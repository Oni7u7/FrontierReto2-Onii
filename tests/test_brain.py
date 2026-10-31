"""tests/test_brain.py — Pruebas de la capa cognitiva sin llamar a OpenRouter real."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.brain import decidir, _parse_actions


# ------------------------------------------------------------------
# Tests de parsing robusto
# ------------------------------------------------------------------

def test_parse_actions_plain_json():
    text = '[{"action": "click", "x": 10, "y": 20}]'
    assert _parse_actions(text) == [{"action": "click", "x": 10, "y": 20}]


def test_parse_actions_with_markdown():
    text = "```json\n[{\"action\": \"done\", \"estado\": \"terminada\", \"resumen\": \"ok\"}]\n```"
    actions = _parse_actions(text)
    assert actions[0]["action"] == "done"
    assert actions[0]["estado"] == "terminada"


def test_parse_actions_with_extra_text():
    text = 'Aquí tienes:\n```\n[{\"action\": \"wait\"}]\n```\n¡Listo!'
    assert _parse_actions(text) == [{"action": "wait"}]


# ------------------------------------------------------------------
# Tests de decidir con mock de httpx
# ------------------------------------------------------------------

def _make_fake_response(content: str) -> MagicMock:
    """Crea un MagicMock que imita un httpx.Response sincrónico."""
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status = MagicMock()
    m.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return m


@pytest.mark.asyncio
async def test_decidir_returns_actions(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    fake_actions = [
        {"action": "click", "x": 100, "y": 200},
        {"action": "type", "text": "hola", "select_all": False},
        {"action": "done", "estado": "terminada", "resumen": "Listo"},
    ]

    async def _fake_post(*args, **kwargs):
        return _make_fake_response(json.dumps(fake_actions))

    with patch("httpx.AsyncClient.post", new=_fake_post):
        result = await decidir("tarea de prueba", b"fake-png", [])

    assert result == fake_actions


@pytest.mark.asyncio
async def test_decidir_retries_on_bad_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    fake_actions = [{"action": "done", "estado": "fallida", "resumen": "error"}]
    call_count = 0

    async def _fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_fake_response("no es json")
        return _make_fake_response(json.dumps(fake_actions))

    with patch("httpx.AsyncClient.post", new=_fake_post):
        result = await decidir("tarea", b"png", [])

    assert call_count == 2
    assert result == fake_actions


@pytest.mark.asyncio
async def test_decidir_raises_on_persistent_bad_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    async def _fake_post(*args, **kwargs):
        return _make_fake_response("nunca json")

    with patch("httpx.AsyncClient.post", new=_fake_post):
        with pytest.raises(RuntimeError, match="JSON válido"):
            await decidir("tarea", b"png", [])


@pytest.mark.asyncio
async def test_no_api_key_raises(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await decidir("tarea", b"png", [])
