"""tests/test_loop.py — Tests del bucle de ejecución con mocks."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import loop

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _fake_png():
    """PNG mínimo válido (1×1 transparente)."""
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452"
        "000000010000000108060000001f15c4"
        "890000000d49444154789c6300000001"
        "000100500dc4530000000049454e44ae"
        "426082"
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-fake")
    monkeypatch.setenv("EQUIPO_TOKEN", "tok-1234567890")
    monkeypatch.setenv("OPENROUTER_MODEL", "fake-model")


@pytest.fixture
def mock_desktop(monkeypatch):
    """Devuelve un par (DesktopMock, patch_ctx) para inyectar."""
    d = MagicMock()
    d.width = 1280
    d.height = 800
    d.connect = AsyncMock()
    d.close = AsyncMock()
    d.screenshot = AsyncMock(return_value=_fake_png())
    d.click = AsyncMock()
    d.double_click = AsyncMock()
    d.type_text = AsyncMock()
    d.press = AsyncMock()
    d.scroll = AsyncMock()
    return d


# ------------------------------------------------------------------
# Test: tarea exitosa corta
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ejecutar_tarea_exitosa(mock_desktop):
    """El modelo devuelve done solo en el turno → terminada."""
    fake_actions = [
        {"action": "click", "x": 100, "y": 200},
        {
            "action": "done",
            "estado": "terminada",
            "resumen": "Cliente creado exitosamente",
        },
    ]
    call_idx = 0

    async def _fake_decidir(*args, **kwargs):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            # Primer turno: click + done (el done debe descartarse)
            return [
                {"action": "click", "x": 100, "y": 200},
                {"action": "done", "estado": "terminada", "resumen": "Listo"},
            ]
        # Segundo turno: done solo (se acepta)
        return [
            {"action": "done", "estado": "terminada", "resumen": "Cliente creado exitosamente"}
        ]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_decidir):
            result = await loop.ejecutar_tarea(
                task_id="T1",
                tarea="Crear cliente",
                ws_url="ws://mock:0",
                password="secreto123",
            )

    assert result["estado"] == "terminada"
    assert "Cliente creado" in result["resumen"]
    assert mock_desktop.click.await_count >= 1
    assert mock_desktop.connect.await_count >= 1
    assert mock_desktop.close.await_count >= 1


# ------------------------------------------------------------------
# Test: corte por tiempo
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_global(mock_desktop, monkeypatch):
    """Si el modelo se cuelga, la tarea falla por timeout del bucle."""
    monkeypatch.setattr(loop, "MAX_TAREA_SEG", 2)   # 2 segundos de límite
    monkeypatch.setattr(loop, "STEP_TIMEOUT", 60)   # timeout por paso grande

    async def _fake_slow(*args, **kwargs):
        await asyncio.sleep(10)  # más que el límite global
        return [{"action": "wait"}]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_slow):
            result = await loop.ejecutar_tarea(
                task_id="T2",
                tarea="Tarea lenta",
                ws_url="ws://mock:0",
                password="secreto123",
            )

    assert result["estado"] == "fallida"
    assert "Tiempo" in result["resumen"] or "agotado" in result["resumen"]
    mock_desktop.close.assert_awaited_once()


# ------------------------------------------------------------------
# Test: fallida ante excepción de VNC
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_excepcion_vnc(mock_desktop):
    """Si Desktop.connect() explota, la tarea es fallida y se cierra."""
    mock_desktop.connect.side_effect = RuntimeError("VNC roto")

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        result = await loop.ejecutar_tarea(
            task_id="T3",
            tarea="Tarea que explota",
            ws_url="ws://mock:0",
            password="secreto123",
        )

    assert result["estado"] == "fallida"
    assert "VNC roto" in result["resumen"]


# ------------------------------------------------------------------
# Test: filtrado de secretos en resumen
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secretos_filtrados(mock_desktop, monkeypatch):
    """La contraseña, token y API key nunca aparecen en el resumen."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real-key-abc123")
    monkeypatch.setenv("EQUIPO_TOKEN", "tok-secret-xyz")

    fake_actions = [
        {
            "action": "done",
            "estado": "fallida",
            "resumen": "Falló porque tok-secret-xyz es inválido y sk-real-key-abc123 expiró",
        }
    ]

    async def _fake_decidir(*args, **kwargs):
        return fake_actions

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_decidir):
            result = await loop.ejecutar_tarea(
                task_id="T4",
                tarea="Tarea con fuga",
                ws_url="ws://mock:0",
                password="clave-vnc-secreta",
            )

    assert result["estado"] == "fallida"
    assert "tok-secret-xyz" not in result["resumen"]
    assert "sk-real-key-abc123" not in result["resumen"]
    assert "clave-vnc-secreta" not in result["resumen"]
    assert "***" in result["resumen"]


# ------------------------------------------------------------------
# Test: detección de bucles
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deteccion_bucle(mock_desktop):
    """Si el modelo repite la misma acción 3 veces con la misma pantalla,
    la tarea termina como fallida."""

    # Siempre devuelve el mismo click
    async def _fake_bucle(*args, **kwargs):
        return [
            {"action": "click", "x": 50, "y": 50},
            {"action": "wait"},
        ]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_bucle):
            result = await loop.ejecutar_tarea(
                task_id="T5",
                tarea="Tarea en bucle",
                ws_url="ws://mock:0",
                password="secreto",
            )

    assert result["estado"] == "fallida"
    assert "bucle" in result["resumen"].lower()


# ------------------------------------------------------------------
# Test: máximo de pasos
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_pasos(mock_desktop, monkeypatch):
    """Si el modelo nunca dice done, el límite de pasos frena la tarea."""
    monkeypatch.setattr(loop, "MAX_PASOS", 3)
    call_idx = 0

    async def _fake_nunca_done(*args, **kwargs):
        nonlocal call_idx
        call_idx += 1
        # Acciones distintas para evitar que el detector de bucle dispare primero
        return [{"action": "click", "x": call_idx, "y": call_idx}]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_nunca_done):
            result = await loop.ejecutar_tarea(
                task_id="T6",
                tarea="Tarea infinita",
                ws_url="ws://mock:0",
                password="secreto",
            )

    assert result["estado"] == "fallida"
    assert "pasos" in result["resumen"].lower() or "agotados" in result["resumen"].lower()


# ------------------------------------------------------------------
# Test: logs escritos y filtrados
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logs_filtrados(mock_desktop, tmp_path, monkeypatch):
    """Las capturas y el log de acciones se guardan, y los secretos se
    reemplazan en el texto."""
    monkeypatch.setattr(loop, "MAX_PASOS", 2)

    log_dir = tmp_path / "logs" / "T7"
    monkeypatch.setattr(loop, "Path", lambda p: tmp_path / p)

    async def _fake_log(*args, **kwargs):
        return [
            {
                "action": "done",
                "estado": "terminada",
                "resumen": "Listo, token secreto: tok-1234567890",
            }
        ]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_log):
            result = await loop.ejecutar_tarea(
                task_id="T7",
                tarea="Tarea",
                ws_url="ws://mock:0",
                password="pwd-789",
            )

    # El resumen filtró el token
    assert "tok-1234567890" not in result["resumen"]
    assert "***" in result["resumen"]


# ------------------------------------------------------------------
# Test: done mezclado con acciones no termina en ese turno
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_done_mezclado_no_termina(mock_desktop):
    """Si el modelo devuelve [click, done] en el primer turno,
    se ejecuta el click, se descarta el done y se vuelve a consultar."""

    call_idx = 0

    async def _fake_mezclado(*args, **kwargs):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return [
                {"action": "click", "x": 200, "y": 300},
                {"action": "done", "estado": "terminada", "resumen": "hecho"},
            ]
        # Segundo consulta: terminada real
        return [{"action": "done", "estado": "terminada", "resumen": "Confirmado"}]

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_mezclado):
            result = await loop.ejecutar_tarea(
                task_id="T8",
                tarea="Tarea mixta",
                ws_url="ws://mock:0",
                password="secreto",
            )

    assert result["estado"] == "terminada"
    assert "Confirmado" in result["resumen"]
    # El click del primer turno SÍ se ejecutó
    assert mock_desktop.click.await_count >= 1
    # Se consultó al modelo al menos DOS veces (primer turno + segundo turno,
    # posiblemente una tercera por verificación final)
    assert call_idx >= 2
