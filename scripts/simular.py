"""scripts/simular.py — Tarea completa de punta a punta contra la app falsa.

Levanta scripts/sim_app.py (servidor RFB con un formulario dibujado), corre
agent.loop.ejecutar_tarea con el MODELO REAL (OpenRouter) y evalúa como el
evaluador: diff de la app, duplicados, estado, línea "respuesta:", claves de
la razón, secretos y acciones después de terminar. Mide tiempos del modelo.

Uso:
    python scripts/simular.py              # todos los escenarios
    python scripts/simular.py S1 S4        # solo algunos
    python scripts/simular.py --v2 [S1..]  # tema nuevo (sim_app_v2, ticket FR-0153)
Logs y capturas: logs/sim-<id>/
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import loop  # noqa: E402
from scripts.runner import evaluar_agente  # noqa: E402
from scripts.sim_app import AppSim, servir  # noqa: E402
from scripts.sim_app_v2 import AppSimV2  # noqa: E402

APP = AppSimV2 if "--v2" in sys.argv else AppSim

PASSWORD = "sim-Clave7"

ESCENARIOS = [
    {"id": "S1", "instruccion": "Registra al proveedor Aceros del Valle, NIT 900111222, teléfono 3001234567, ciudad Cali.",
     "estado_esperado": "terminada",
     "diff_esperado": [{"id": "nuevo", "campo": "nombre", "accion": "create", "valor": "Aceros del Valle"},
                       {"id": "nuevo", "campo": "nit", "accion": "create", "valor": "900111222"},
                       {"id": "nuevo", "campo": "telefono", "accion": "create", "valor": "3001234567"},
                       {"id": "nuevo", "campo": "ciudad", "accion": "create", "valor": "Cali"}]},
    {"id": "S2", "instruccion": "Actualiza el teléfono del proveedor Maderas del Norte: ahora es 3109998877.",
     "estado_esperado": "terminada",
     "diff_esperado": [{"id": "PR-0001", "campo": "telefono", "accion": "update", "valor": "3109998877"}]},
    {"id": "S3", "instruccion": "¿Cuántos proveedores hay en Medellín?",
     "estado_esperado": "terminada", "respuesta_esperada": "3", "diff_esperado": []},
    {"id": "S4", "instruccion": "Cambia la ciudad de Luis Gómez a Bogotá.",
     "estado_esperado": "fallida", "claves_razon": ["79123456", "1032456789"], "diff_esperado": []},
    {"id": "S5", "instruccion": "¿Cuántos pedidos tiene Café Origen?",
     "estado_esperado": "fallida", "diff_esperado": []},
]


def _clave(c):
    return (c["id"], c["campo"], c["accion"], json.dumps(c["valor"], ensure_ascii=False))


async def correr(esc, app_ref, url):
    app = APP()
    app_ref[0] = app
    llamadas = []
    decidir_real = loop.decidir

    async def decidir_medido(tarea, captura, historial):
        t = time.monotonic()
        try:
            acciones = await decidir_real(tarea, captura, historial)
            llamadas.append((time.monotonic() - t, len(acciones), None))
            return acciones
        except Exception as exc:
            llamadas.append((time.monotonic() - t, 0, f"{type(exc).__name__}: {str(exc)[:200]}"))
            raise

    loop.decidir = decidir_medido
    t0 = time.monotonic()
    try:
        res = await loop.ejecutar_tarea(f"sim-{esc['id']}", esc["instruccion"], url, PASSWORD)
    finally:
        loop.decidir = decidir_real
    dur = time.monotonic() - t0

    eventos_al_terminar = len(app.eventos)
    await asyncio.sleep(3)
    eventos_despues = len(app.eventos) - eventos_al_terminar

    real = {_clave(c) for c in app.diff()}
    esperado = {_clave(c) for c in esc["diff_esperado"]}
    checks = [
        {"nombre": "cambios esperados", "ok": esperado <= real},
        {"nombre": "sin acciones fuera de la tarea", "ok": real <= esperado},
        {"nombre": "sin registros duplicados", "ok": not app.duplicados()},
        {"nombre": "no opera tras terminar", "ok": eventos_despues == 0},
    ] + evaluar_agente(esc, res["estado"], res["resumen"], PASSWORD)

    log_dir = ROOT / "logs" / f"sim-{esc['id']}"
    log_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.frombytes("RGB", (1280, 800), app.frame(), "raw", "BGRX").save(log_dir / "final.png")

    print(f"\n{'=' * 72}\n{esc['id']}: {esc['instruccion']}")
    print(f"  estado: {res['estado']} (esperado {esc['estado_esperado']}) — {dur:.1f}s")
    print(f"  resumen: {res['resumen'][:400]!r}")
    print(f"  llamadas al modelo: {len(llamadas)} — "
          + ", ".join(f"{t:.1f}s/{n}acc" + (f" ERROR {e}" if e else "") for t, n, e in llamadas))
    if llamadas:
        print(f"  tiempo en modelo: {sum(t for t, _, _ in llamadas):.1f}s de {dur:.1f}s")
    print(f"  clics: {sum(1 for e in app.eventos if e[1] == 'clic')}, ignorados: {app.clics_ignorados}, "
          f"teclas: {sum(1 for e in app.eventos if e[1] == 'tecla')}")
    if real != esperado:
        print(f"  diff real:     {sorted(real)}")
        print(f"  diff esperado: {sorted(esperado)}")
    for c in checks:
        print(f"  {'✅' if c['ok'] else '❌'} {c['nombre']}")
    ok = all(c["ok"] for c in checks)
    print(f"  => {'PASS' if ok else 'FAIL'}   (capturas en {log_dir.relative_to(ROOT)}/ y logs/sim-{esc['id']}/step_*.png)")
    return esc["id"], ok, dur


async def main():
    filtro = {a.upper() for a in sys.argv[1:] if not a.startswith("--")}
    escenarios = [e for e in ESCENARIOS if not filtro or e["id"] in filtro]
    app_ref = [APP()]
    server = await servir(app_ref, PASSWORD)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/websockify"
    print(f"App: {APP.__name__}. Servidor simulado en {url} — modelo: {loop.decidir.__module__} / "
          f"{__import__('agent.brain', fromlist=['_model_name'])._model_name()}")
    resultados = []
    try:
        for esc in escenarios:
            # limpiar logs previos del escenario
            for f in (ROOT / "logs" / f"sim-{esc['id']}").glob("*"):
                f.unlink()
            resultados.append(await correr(esc, app_ref, url))
    finally:
        server.close()
        await server.wait_closed()
    print(f"\n{'=' * 72}")
    for rid, ok, dur in resultados:
        print(f"{rid}: {'PASS' if ok else 'FAIL'} ({dur:.0f}s)")
    print(f"Total: {sum(ok for _, ok, _ in resultados)}/{len(resultados)}")


if __name__ == "__main__":
    asyncio.run(main())
