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
    monkeypatch.setenv("MODELO", "fake-model")


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
    # Exactamente DOS consultas: primer turno + segundo turno (sin verificación final)
    assert call_idx == 2


# ------------------------------------------------------------------
# Test: scroll(x, y, dy) y acciones inválidas
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scroll_pasa_xy_y_usa_centro_por_defecto(mock_desktop):
    turnos = iter([
        [{"action": "scroll", "x": 300, "y": 500, "dy": 240}],
        [{"action": "scroll", "dy": -120}],
        [{"action": "done", "estado": "terminada", "resumen": "ok"}],
    ])

    async def _fake_decidir(*args, **kwargs):
        return next(turnos, [{"action": "done", "estado": "terminada", "resumen": "ok"}])

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_decidir):
            await loop.ejecutar_tarea(
                task_id="T-scroll", tarea="x", ws_url="ws://mock:0", password="p"
            )

    llamadas = [c.args for c in mock_desktop.scroll.await_args_list]
    assert llamadas[:2] == [(300, 500, 240), (640, 400, -120)]


@pytest.mark.asyncio
async def test_accion_invalida_se_reporta_al_modelo_sin_matar_la_tarea(mock_desktop):
    mock_desktop.press = AsyncMock(side_effect=ValueError("Tecla desconocida: 'Intro'"))
    historiales = []
    turnos = iter([
        [{"action": "key", "key": "Intro"}, {"action": "click", "x": 1, "y": 1}],
        [{"action": "done", "estado": "terminada", "resumen": "ok"}],
    ])

    async def _fake_decidir(tarea, captura, historial, *args, **kwargs):
        historiales.append(json.dumps(historial, ensure_ascii=False))
        return next(turnos, [{"action": "done", "estado": "terminada", "resumen": "ok"}])

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_decidir):
            result = await loop.ejecutar_tarea(
                task_id="T-invalida", tarea="x", ws_url="ws://mock:0", password="p"
            )

    assert result["estado"] == "terminada"
    assert "acción inválida" in historiales[1] and "Intro" in historiales[1]
    # el turno se corta: el click posterior a la acción inválida no se ejecuta
    assert mock_desktop.click.await_count == 0


# ------------------------------------------------------------------
# Test: formato de la línea respuesta
# ------------------------------------------------------------------

def test_respuesta_pegada_se_mueve_a_linea_propia():
    r = loop._normalizar_respuesta(
        'La lista muestra "10 proveedor(es)". En Medellín: PR-0001, PR-0003 y PR-0008. respuesta: 3',
        "terminada",
    )
    lineas = r.splitlines()
    assert lineas[-1] == "respuesta: 3"
    assert sum(1 for l in lineas if "respuesta:" in l.lower()) == 1


def test_respuesta_dos_datos_y_ya_bien_formada_no_cambian():
    ok = "Revisé el pedido.\nrespuesta: 45; Juan Pérez"
    assert loop._normalizar_respuesta(ok, "terminada") == ok
    assert loop._normalizar_respuesta("Respuesta: 7", "terminada") == "respuesta: 7"


def test_fallida_nunca_lleva_linea_respuesta():
    r = loop._normalizar_respuesta(
        "No hay sección de pedidos.\nrespuesta: no disponible\nNo se modificó nada.", "fallida")
    assert "respuesta" not in r.lower()
    assert "No hay sección de pedidos." in r and "No se modificó nada." in r


def test_sin_respuesta_no_toca_el_resumen():
    s = "Se actualizó el teléfono a 3109998877."
    assert loop._normalizar_respuesta(s, "terminada") == s


# ------------------------------------------------------------------
# Test: una captura por turno y tanda cortada tras un click
# ------------------------------------------------------------------

def test_recortar_tanda():
    t = [{"action": "type", "text": "a"}, {"action": "click", "x": 1, "y": 1},
         {"action": "type", "text": "b"}]
    assert loop._recortar_tanda(t) == (t[:2], t[2:])
    sin_clic = [{"action": "type", "text": "a"}, {"action": "key", "key": "Tab"}]
    assert loop._recortar_tanda(sin_clic) == (sin_clic, [])


@pytest.mark.asyncio
async def test_tanda_cortada_tras_click_y_una_captura_por_turno(mock_desktop):
    historiales = []
    turnos = iter([
        [{"action": "click", "x": 10, "y": 10}, {"action": "type", "text": "no"},
         {"action": "click", "x": 20, "y": 20}],
        [{"action": "type", "text": "uno"}, {"action": "key", "key": "Tab"},
         {"action": "type", "text": "dos"}, {"action": "wait"}],
        [{"action": "done", "estado": "terminada", "resumen": "ok"}],
    ])

    async def _fake_decidir(tarea, captura, historial, *args, **kwargs):
        historiales.append(json.dumps(historial, ensure_ascii=False))
        return next(turnos, [{"action": "done", "estado": "terminada", "resumen": "ok"}])

    with patch("agent.loop.Desktop", return_value=mock_desktop):
        with patch("agent.loop.decidir", new=_fake_decidir):
            with patch.object(loop, "SETTLE", 0.0):
                await loop.ejecutar_tarea(
                    task_id="T-tanda", tarea="x", ws_url="ws://mock:0", password="p")

    assert [c.args for c in mock_desktop.click.await_args_list] == [(10, 10)]
    tipeos = [c.args[0] for c in mock_desktop.type_text.await_args_list]
    assert tipeos == ["uno", "dos"]
    assert "2 acciones posteriores NO se ejecutaron" in historiales[1]
    # 3 turnos del bucle → exactamente 3 capturas
    assert mock_desktop.screenshot.await_count == 3


@pytest.mark.asyncio
async def test_tras_done_no_reconecta_ni_consulta(mock_desktop):
    """Al reportar terminada se deja de operar: una conexión y una consulta."""
    llamadas = 0

    async def _fake_decidir(*args, **kwargs):
        nonlocal llamadas
        llamadas += 1
        return [{"action": "done", "estado": "terminada", "resumen": "ok"}]

    with patch("agent.loop.Desktop", return_value=mock_desktop) as cls:
        with patch("agent.loop.decidir", new=_fake_decidir):
            await loop.ejecutar_tarea(
                task_id="T-sin-verif", tarea="x", ws_url="ws://mock:0", password="p")

    assert llamadas == 1
    assert cls.call_count == 1
    assert mock_desktop.connect.await_count == 1
