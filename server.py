"""
server.py — Entrypoint FastAPI del agente.

Expone:
- GET  /health          → 200 cuando el agente está listo
- POST /tasks          → Recibe tarea + escritorio, devuelve task_id
- GET  /tasks/{id}     → Estado y resumen de la tarea
"""
from fastapi import FastAPI

app = FastAPI(title="SIGECOM Agent")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# TODO (Fase 3+): POST /tasks y GET /tasks/{task_id}
