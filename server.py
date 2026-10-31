"""
server.py — FastAPI entrypoint del agente.

Rutas:
  GET  /health          → 200 cuando el agente está listo
  POST /tasks          → recibe tarea + escritorio, devuelve task_id (< 2 s)
  GET  /tasks/{id}     → estado + resumen de la tarea

Todas las rutas exigen Authorization: Bearer <EQUIPO_TOKEN> (comparado
con hmac.compare_digest para evitar timing attacks).

Estado en memoria. Una sola tarea activa a la vez; si llega otra se
CANCELA la anterior (marca fallida con "cancelada por tarea nueva") y
se arranca la nueva.
"""

import asyncio
import hmac
import os
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.loop import ejecutar_tarea

app = FastAPI(title="SIGECOM Agent")

# ------------------------------------------------------------------
# Estado en memoria
# ------------------------------------------------------------------

_TASKS: dict[str, dict[str, Any]] = {}
_CURRENT_TASK_ID: str | None = None
_CURRENT_ASYNCIO_TASK: asyncio.Task | None = None
_LOCK = asyncio.Lock()


# ------------------------------------------------------------------
# Modelos Pydantic
# ------------------------------------------------------------------

class Escritorio(BaseModel):
    url: str
    password: str


class TaskIn(BaseModel):
    tarea: str
    escritorio: Escritorio


class TaskStatus(BaseModel):
    estado: str
    resumen: str


# ------------------------------------------------------------------
# Auth helper
# ------------------------------------------------------------------

def _validar_token(authorization: str | None) -> None:
    token_esperado = os.getenv("EQUIPO_TOKEN", "")
    expected = f"Bearer {token_esperado}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ------------------------------------------------------------------
# Runner de tarea
# ------------------------------------------------------------------

async def _run_task(task_id: str, tarea: str, ws_url: str, password: str) -> None:
    """Corre una tarea y guarda el resultado en _TASKS."""
    try:
        resultado = await ejecutar_tarea(task_id, tarea, ws_url, password)
    except asyncio.CancelledError:
        resultado = {"estado": "fallida", "resumen": "cancelada por tarea nueva"}
    except Exception as exc:
        resultado = {
            "estado": "fallida",
            "resumen": f"Error interno del agente: {type(exc).__name__}: {exc}",
        }

    # Filtrar contraseña del escritorio del resumen
    resumen = resultado.get("resumen", "")
    if password and password in resumen:
        resumen = resumen.replace(password, "***")

    async with _LOCK:
        if _TASKS.get(task_id, {}).get("estado") == "en_curso":
            _TASKS[task_id]["estado"] = resultado["estado"]
            _TASKS[task_id]["resumen"] = resumen


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict:
    _validar_token(authorization)
    return {"status": "ok"}


@app.post("/tasks")
async def create_task(
    payload: TaskIn,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _validar_token(authorization)

    global _CURRENT_ASYNCIO_TASK, _CURRENT_TASK_ID
    task_id = str(uuid.uuid4())
    async with _LOCK:
        # Cancelar tarea anterior si existe
        if _CURRENT_ASYNCIO_TASK is not None and not _CURRENT_ASYNCIO_TASK.done():
            _CURRENT_ASYNCIO_TASK.cancel()
            if _CURRENT_TASK_ID and _CURRENT_TASK_ID in _TASKS:
                _TASKS[_CURRENT_TASK_ID]["estado"] = "fallida"
                _TASKS[_CURRENT_TASK_ID]["resumen"] = "cancelada por tarea nueva"

        _TASKS[task_id] = {
            "estado": "en_curso",
            "resumen": "",
            "tarea": payload.tarea,
            "ws_url": payload.escritorio.url,
            "password": payload.escritorio.password,
        }
        _CURRENT_TASK_ID = task_id
        _CURRENT_ASYNCIO_TASK = asyncio.create_task(
            _run_task(task_id, payload.tarea, payload.escritorio.url, payload.escritorio.password)
        )

    return JSONResponse(content={"task_id": task_id}, status_code=202)


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    authorization: str | None = Header(default=None),
) -> TaskStatus:
    _validar_token(authorization)

    async with _LOCK:
        info = _TASKS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskStatus(estado=info["estado"], resumen=info.get("resumen", ""))
