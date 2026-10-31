"""
agent/loop.py — Bucle principal de ejecución de tareas.

ejecutar_tarea(tarea, ws_url, password):
  1. Conecta Desktop.
  2. Bucle: screenshot(settle=1.3) → decidir → ejecutar → repetir.
  3. Límite duro 4 m 15 s (255 s).
  4. Detección de bucles (misma acción + screenshot igual ×3).
  5. Filtro de secretos en resumen y logs.
  6. Verificación final opcional (conservadora: no invierte terminada).
  7. Al terminar o fallar cierra Desktop inmediatamente.
"""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from agent.brain import decidir
from agent.vnc import Desktop


# ------------------------------------------------------------------
# Configuración
# ------------------------------------------------------------------

MAX_TAREA_SEG = 255          # 4 min 15 s
MAX_PASOS = 50               # límite de iteraciones por tarea
SETTLE = 1.3                 # segundos de espera tras cada acción
STEP_TIMEOUT = 90.0          # máximo para una llamada al modelo

_SECRETOS = ["OPENROUTER_API_KEY", "EQUIPO_TOKEN"]


def _filtrar_secretos(texto: str) -> str:
    """Reemplaza en el string cualquier valor de variable sensible por ***."""
    for nombre in _SECRETOS:
        valor = os.getenv(nombre)
        if valor and valor in texto:
            texto = texto.replace(valor, "***")
    # Además, cualquier clave API-like genérica
    texto = _borrar_api_keys(texto)
    return texto


def _borrar_api_keys(texto: str) -> str:
    """Regex simple para claves tipo sk-or-..., bearer tokens largos."""
    import re
    texto = re.sub(r"\bsk-[a-zA-Z0-9_-]{20,}\b", "***", texto)
    texto = re.sub(r"\b[A-Za-z0-9+/]{40,}=?=?\b", lambda m: "***" if len(m.group()) > 40 else m.group(), texto)
    return texto


async def ejecutar_tarea(
    task_id: str,
    tarea: str,
    ws_url: str,
    password: str,
) -> dict[str, Any]:
    """
    Ejecuta una tarea completa.

    Retorna dict con:
        estado  -> "en_curso" | "terminada" | "fallida"
        resumen -> str
    """
    log_dir = Path("logs") / task_id
    log_dir.mkdir(parents=True, exist_ok=True)

    desktop = Desktop(ws_url, password)
    start = time.monotonic()
    historial: list[dict] = []
    paso = 0

    # Para detección de bucles
    ultima_accion_json: str | None = None
    ultimos_hashes: list[str] = []
    repeticiones_bucle = 0

    estado = "en_curso"
    resumen = ""
    _closed = False

    def _queda() -> float:
        return MAX_TAREA_SEG - (time.monotonic() - start)

    def _salvar_paso(captura: bytes, accion: dict, txt: str) -> None:
        """Guarda captura + descripción en disco, filtrando secretos."""
        n = str(paso).zfill(3)
        (log_dir / f"step_{n}.png").write_bytes(captura)
        txt_filtrado = _filtrar_secretos(txt)
        with open(log_dir / "acciones.txt", "a", encoding="utf-8") as fh:
            fh.write(f"[{n}] {accion} -> {txt_filtrado}\n")

    async def _cerrar() -> None:
        nonlocal _closed
        if not _closed:
            _closed = True
            try:
                await desktop.close()
            except Exception:
                pass

    async def _screenshot() -> bytes:
        """Captura con settle=1.3, envuelta en timeout propio."""
        return await asyncio.wait_for(
            desktop.screenshot(settle=SETTLE),
            timeout=max(5.0, _queda()),
        )

    async def _decidir(captura: bytes) -> list[dict]:
        """Llama al modelo con timeout por-paso."""
        return await asyncio.wait_for(
            decidir(tarea, captura, historial),
            timeout=min(STEP_TIMEOUT, max(5.0, _queda())),
        )

    try:
        # ---------- conectar ----------
        await asyncio.wait_for(desktop.connect(), timeout=10.0)

        while estado == "en_curso" and paso < MAX_PASOS and _queda() > 5.0:
            paso += 1

            # 1. captura
            captura = await _screenshot()

            # 2. decidir
            acciones = await _decidir(captura)

            # 3. ejecutar secuencialmente
            for accion in acciones:
                if estado != "en_curso":
                    break

                accion_str = json.dumps(accion, ensure_ascii=False)

                # detección de bucle
                if accion == acciones[-1] and accion.get("action") != "done":
                    h = hashlib.sha256(captura).hexdigest()[:16]
                    act_hash = f"{accion_str}_{h}"
                    if act_hash == ultima_accion_json:
                        repeticiones_bucle += 1
                        if repeticiones_bucle >= 2:  # 3 veces en total (0,1,2)
                            estado = "fallida"
                            resumen = (
                                "La tarea entró en bucle: la pantalla y las acciones "
                                f"no cambiaron tras {repeticiones_bucle + 1} intentos."
                            )
                            break
                    else:
                        repeticiones_bucle = 0
                        ultima_accion_json = act_hash

                # ejecutar
                await _ejecutar_accion(accion, desktop, _queda)
                _salvar_paso(captura, accion, accion_str)

                # screenshot después de cada acción para estabilizar
                if accion.get("action") != "done":
                    captura = await _screenshot()

                if accion.get("action") == "done":
                    estado = accion.get("estado", "fallida")
                    resumen = accion.get("resumen", "")
                    break

        # ---------- post-bucle ----------
        if estado == "en_curso":
            estado = "fallida"
            resumen = f"Tiempo o pasos agotados ({paso} pasos, {255 - int(_queda())} s)."

    except asyncio.TimeoutError:
        estado = "fallida"
        resumen = "Tiempo de ejecución agotado (4 min 15 s)."
    except Exception as exc:
        estado = "fallida"
        resumen = f"Error durante la ejecución: {type(exc).__name__}: {exc}"
    finally:
        await _cerrar()

    # ---------- filtrar secretos ----------
    resumen = _filtrar_secretos(resumen)

    # ---------- verificación final (conservadora) ----------
    if estado == "terminada":
        # Solo si queda tiempo suficiente
        if _queda() > 10.0 and os.getenv("OPENROUTER_API_KEY"):
            try:
                # Captura final
                desktop2 = Desktop(ws_url, password)
                await asyncio.wait_for(desktop2.connect(), timeout=10.0)
                final_img = await asyncio.wait_for(
                    desktop2.screenshot(settle=SETTLE), timeout=10.0
                )
                verif = await asyncio.wait_for(
                    decidir(
                        f"Verificación: ¿la tarea '{tarea}' quedó realmente resuelta? "
                        "Responde SOLO un JSON: {{\"ok\": true}} o {{\"ok\": false, \"motivo\": \"...\"}}.",
                        final_img,
                        historial,
                    ),
                    timeout=min(STEP_TIMEOUT, max(5.0, _queda())),
                )
                await desktop2.close()
                # Conservador: solo marcamos fallida si el modelo dice explícitamente ok:false
                if verif and verif[-1].get("action") == "done":
                    verif_resumen = verif[-1].get("resumen", "")
                    if "ok: false" in verif_resumen.lower() or '"ok": false' in verif_resumen.lower():
                        estado = "fallida"
                        resumen = f"Verificación final indicó que la tarea no quedó completa: {verif_resumen}"
            except Exception:
                # Si la verificación falla por red/VNC/modelo, mantenemos terminada
                pass
            finally:
                try:
                    await desktop2.close()
                except Exception:
                    pass

    return {"estado": estado, "resumen": resumen}


async def _ejecutar_accion(
    accion: dict,
    desktop: Desktop,
    queda_fn,
) -> None:
    """Ejecuta una única acción dict sobre Desktop."""
    if queda_fn() <= 0:
        raise asyncio.TimeoutError("tiempo agotado")

    tipo = accion.get("action")

    if tipo == "click":
        await desktop.click(accion["x"], accion["y"])
    elif tipo == "double_click":
        await desktop.double_click(accion["x"], accion["y"])
    elif tipo == "type":
        await desktop.type_text(accion["text"], accion.get("select_all", True))
    elif tipo == "key":
        await desktop.press(accion["key"])
    elif tipo == "scroll":
        await desktop.scroll(accion["dy"])
    elif tipo == "wait":
        await asyncio.sleep(SETTLE)
    elif tipo == "done":
        pass  # se maneja en el bucle superior
    else:
        raise ValueError(f"Acción desconocida: {tipo}")
