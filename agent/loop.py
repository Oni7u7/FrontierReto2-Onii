"""
agent/loop.py — Bucle principal de ejecución de tareas.

ejecutar_tarea(tarea, ws_url, password):
  1. Conecta Desktop.
  2. Bucle: screenshot(settle=1.3) → decidir → ejecutar → repetir.
     Una sola captura por turno: la tanda se corta tras el primer
     click/double_click/scroll (lo de detrás se descarta y se anota).
  3. Si un turno trae acciones + done: ejecuta las acciones, descarta
     el done, toma captura nueva y vuelve a consultar.
     Solo se acepta done cuando va SOLO en el turno.
     Excepción: done con estado fallida y sin acciones previas se acepta.
  4. Límite duro 4 m 15 s (255 s).
  5. Detección de bucles (misma acción + screenshot igual ×3).
  6. Filtro de secretos en resumen y logs (incluye password del escritorio).
  7. Al terminar, cancelar o fallar cierra Desktop inmediatamente.
"""

import asyncio
import hashlib
import json
import os
import re
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
# Acciones que pueden cambiar de pantalla: la tanda se corta después de ellas
# y lo que venía detrás se descarta (apuntaría a una pantalla que ya no existe).
CORTA_TANDA = {"click", "double_click", "scroll"}

_SECRETOS_ENV = ["OPENROUTER_API_KEY", "EQUIPO_TOKEN"]


def _filtrar_secretos(texto: str, extra: list[str] | None = None) -> str:
    """Reemplaza en el string cualquier valor de variable sensible por ***."""
    for nombre in _SECRETOS_ENV:
        valor = os.getenv(nombre)
        if valor and valor in texto:
            texto = texto.replace(valor, "***")
    if extra:
        for val in extra:
            if val and val in texto:
                texto = texto.replace(val, "***")
    texto = _borrar_api_keys(texto)
    return texto


_RE_RESPUESTA = re.compile(r"respuesta\s*:", re.IGNORECASE)


def _normalizar_respuesta(resumen: str, estado: str) -> str:
    """Deja la respuesta como lo exige el evaluador.

    - terminada: si "respuesta:" va pegada a otro texto, se mueve a una línea
      propia al final ("respuesta: <valor>"), para que el primer número de esa
      línea sea el valor y no otro dato del resumen.
    - fallida: se quita cualquier línea "respuesta:" (una pregunta imposible
      no la lleva y una tarea que no es pregunta nunca la necesita).
    """
    m = _RE_RESPUESTA.search(resumen)
    if not m:
        return resumen
    fin_linea = resumen.find("\n", m.end())
    fin_linea = len(resumen) if fin_linea == -1 else fin_linea
    valor = resumen[m.end():fin_linea].strip()
    resto = (resumen[:m.start()].rstrip() + resumen[fin_linea:]).strip()
    if estado != "terminada" or not valor:
        return resto
    return f"{resto}\nrespuesta: {valor}" if resto else f"respuesta: {valor}"


def _recortar_tanda(acciones: list[dict]) -> tuple[list[dict], list[dict]]:
    """Corta la tanda después de la primera acción de CORTA_TANDA.

    Devuelve (a_ejecutar, descartadas). type/key/wait no cortan.
    """
    for i, accion in enumerate(acciones):
        if accion.get("action") in CORTA_TANDA:
            return acciones[: i + 1], acciones[i + 1:]
    return acciones, []


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
    total_acciones_ejecutadas = 0

    # Para detección de bucles
    ultima_accion_json: str | None = None
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
        txt_filtrado = _filtrar_secretos(txt, extra=[password])
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

            # 3. separar done del resto
            done_actions = [a for a in acciones if a.get("action") == "done"]
            otras_acciones = [a for a in acciones if a.get("action") != "done"]

            if done_actions and otras_acciones:
                # done mezclado → ejecutar solo acciones, descartar done
                acciones_a_ejecutar = otras_acciones
                historial.append({
                    "nota": "done descartado: había acciones sin ejecutar, se ejecutaron primero"
                })
            elif done_actions and not otras_acciones:
                # done solo → aceptar
                acciones_a_ejecutar = [done_actions[0]]
            else:
                acciones_a_ejecutar = acciones

            if acciones_a_ejecutar and acciones_a_ejecutar[0].get("action") != "done":
                acciones_a_ejecutar, descartadas = _recortar_tanda(acciones_a_ejecutar)
                if descartadas:
                    historial.append({
                        "nota": (
                            f"turno cortado tras {acciones_a_ejecutar[-1].get('action')}: "
                            f"{len(descartadas)} acciones posteriores NO se ejecutaron "
                            "porque la pantalla pudo cambiar; decide de nuevo con la captura nueva"
                        )
                    })

            # 4. ejecutar secuencialmente
            for idx, accion in enumerate(acciones_a_ejecutar):
                if estado != "en_curso":
                    break

                accion_str = json.dumps(accion, ensure_ascii=False)

                # detección de bucle: solo la ÚLTIMA acción del turno
                es_ultima = (idx == len(acciones_a_ejecutar) - 1)
                if es_ultima and accion.get("action") != "done":
                    h = hashlib.sha256(captura).hexdigest()[:16]
                    act_hash = f"{accion_str}_{h}"
                    if act_hash == ultima_accion_json:
                        repeticiones_bucle += 1
                        if repeticiones_bucle >= 2:
                            estado = "fallida"
                            resumen = (
                                "La tarea entró en bucle: la pantalla y las acciones "
                                f"no cambiaron tras {repeticiones_bucle + 1} intentos."
                            )
                            break
                    else:
                        repeticiones_bucle = 0
                        ultima_accion_json = act_hash

                # ejecutar; una acción mal formada (tecla desconocida, campo
                # faltante) se reporta al modelo y se corta el turno
                try:
                    # un wait al final sobra: la captura del turno ya espera SETTLE
                    if not (es_ultima and accion.get("action") == "wait"):
                        await _ejecutar_accion(accion, desktop, _queda)
                except (ValueError, KeyError, TypeError) as exc:
                    historial.append({
                        "nota": f"acción inválida no ejecutada: {accion_str} ({exc})"
                    })
                    break
                _salvar_paso(captura, accion, accion_str)
                historial.append(accion)
                total_acciones_ejecutadas += 1

                if accion.get("action") == "done":
                    estado = accion.get("estado", "fallida")
                    resumen = accion.get("resumen", "")
                    break

        # ---------- post-bucle ----------
        if estado == "en_curso":
            estado = "fallida"
            resumen = f"Tiempo o pasos agotados ({paso} pasos, {255 - int(_queda())} s)."

    except asyncio.CancelledError:
        estado = "fallida"
        resumen = "cancelada por tarea nueva"
    except asyncio.TimeoutError:
        estado = "fallida"
        resumen = "Tiempo de ejecución agotado (4 min 15 s)."
    except Exception as exc:
        estado = "fallida"
        resumen = f"Error durante la ejecución: {type(exc).__name__}: {exc}"
    finally:
        await _cerrar()

    # ---------- formato de respuesta + filtrar secretos (incluye password) ----------
    resumen = _normalizar_respuesta(resumen, estado)
    resumen = _filtrar_secretos(resumen, extra=[password])

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
        # sin x,y el scroll cae en el centro de la pantalla
        await desktop.scroll(
            accion.get("x", desktop.width // 2 or 640),
            accion.get("y", desktop.height // 2 or 400),
            accion["dy"],
        )
    elif tipo == "wait":
        await asyncio.sleep(SETTLE)
    elif tipo == "done":
        pass
    else:
        raise ValueError(f"Acción desconocida: {tipo}")
