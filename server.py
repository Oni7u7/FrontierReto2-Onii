"""
server.py — FastAPI entrypoint del agente.

Rutas:
  GET  /health          → 200 cuando el agente está listo
  POST /tasks          → recibe tarea + escritorio, devuelve task_id (< 2 s)
  GET  /tasks/{id}     → estado + resumen de la tarea

Todas las rutas exigen Authorization: Bearer <EQUIPO_TOKEN>.
El estado se guarda en memoria. Una sola tarea activa a la vez;
si llega otra se encola.
"""

import asyncio
import os
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.loop import ejecutar_tarea

app = FastAPI(title="SIGECOM Agent")

# ------------------------------------------------------------------
# Estado en memoria
# ------------------------------------------------------------------

_TASKS: dict[str, dict[str, Any]] = {}          # task_id → {estado, resumen, task}
_TASK_QUEUE: asyncio.Queue[str] = asyncio.Queue()  # cola de task_ids pendientes
_CURRENT_TASK: asyncio.Task | None = None
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


class TaskOut(BaseModel):
    task_id: str


class TaskStatus(BaseModel):
    estado: str
    resumen: str


# ------------------------------------------------------------------
# Auth helper
# ------------------------------------------------------------------

def _validar_token(authorization: str | None) -> None:
    token_esperado = os.getenv("EQUIPO_TOKEN", "")
    expected = f"Bearer {token_esperado}" if token_esperado else None
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ------------------------------------------------------------------
# Tarea worker (corre una tarea de la cola)
# ------------------------------------------------------------------

async def _worker() -> None:
    global _CURRENT_TASK
    while True:
        task_id = await _TASK_QUEUE.get()
        async with _LOCK:
            if _TASKS.get(task_id, {}).get("estado") != "encolada":
                continue  # cancelada o ya procesada
            _TASKS[task_id]["estado"] = "en_curso"

        tarea = _TASKS[task_id]["tarea"]
        ws_url = _TASKS[task_id]["ws_url"]
        password = _TASKS[task_id]["password"]

        try:
            resultado = await ejecutar_tarea(task_id, tarea, ws_url, password)
        except Exception as exc:
            resultado = {
                "estado": "fallida",
                "resumen": f"Error interno del agente: {type(exc).__name__}: {exc}",
            }

        async with _LOCK:
            _TASKS[task_id]["estado"] = resultado["estado"]
            _TASKS[task_id]["resumen"] = resultado["resumen"]
            _CURRENT_TASK = None

        _TASK_QUEUE.task_done()

        # Lanza la siguiente si hay algo en cola
        await _maybe_start_next()


async def _maybe_start_next() -> None:
    global _CURRENT_TASK
    async with _LOCK:
        if _CURRENT_TASK is not None:
            return
        if _TASK_QUEUE.empty():
            return
        # No sacamos aquí; _worker hace get()
        _CURRENT_TASK = asyncio.create_task(_worker())


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

    task_id = str(uuid.uuid4())
    async with _LOCK:
        _TASKS[task_id] = {
            "estado": "encolada",
            "resumen": "",
            "tarea": payload.tarea,
            "ws_url": payload.escritorio.url,
            "password": payload.escritorio.password,
        }

    await _TASK_QUEUE.put(task_id)
    await _maybe_start_next()

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
