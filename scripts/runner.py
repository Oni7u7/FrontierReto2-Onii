"""scripts/runner.py — Ejecuta todas las pruebas de pruebas/ contra el agente.

Para cada JSON:
  1. POST /__reset al escritorio
  2. POST /tasks al agente local
  3. Espera hasta que la tarea no esté en_curso
  4. POST /__check con el JSON de la prueba
  5. Muestra tabla de resultados

Lee la configuración de .env: ESCRITORIO_WS (wss://.../websockify, tal cual),
ESCRITORIO_PASSWORD, ESCRITORIO_TOKEN, EQUIPO_TOKEN y opcional AGENTE_URL.
La URL https para /__reset y /__check se deriva de ESCRITORIO_WS.

Además de /__check (estado de la app) evalúa localmente lo que el evaluador
mira aparte: estado esperado, línea "respuesta:", claves_razon y secretos.

Uso:
    python scripts/runner.py [E1 E3 ...]   # sin argumentos corre todas
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import re

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override=True: el .env manda. Sin él, variables viejas exportadas en el shell
# (p. ej. las del servidor simulado en 127.0.0.1) ganan en silencio y el agente
# recibiría ese escritorio en vez del real.
load_dotenv(ROOT / ".env", override=True)

PRUEBAS = Path(__file__).resolve().parent.parent / "pruebas"



def base_https(ws_url: str) -> str:
    """wss://host/ruta/websockify -> https://host/ruta (ws:// -> http://)."""
    url = ws_url.strip().rstrip("/")
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    if url.endswith("/websockify"):
        url = url[: -len("/websockify")]
    return url.rstrip("/")


AGENTE_URL = os.getenv("AGENTE_URL", "http://127.0.0.1:8000")
ESCRITORIO_WS = os.getenv("ESCRITORIO_WS", "")
ESCRITORIO_URL = base_https(ESCRITORIO_WS) if ESCRITORIO_WS else ""
ESCRITORIO_TOKEN = os.getenv("ESCRITORIO_TOKEN", "")
ESCRITORIO_PASSWORD = os.getenv("ESCRITORIO_PASSWORD", "")
EQUIPO_TOKEN = os.getenv("EQUIPO_TOKEN", "")

POLL_INTERVAL = 5   # segundos entre consultas (igual que el evaluador)


def _check_env():
    missing = []
    for name in ("ESCRITORIO_WS", "ESCRITORIO_TOKEN", "ESCRITORIO_PASSWORD", "EQUIPO_TOKEN"):
        if not os.getenv(name):
            missing.append(name)
    if missing:
        print(f"❌ Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)
    from urllib.parse import urlsplit
    if urlsplit(ESCRITORIO_WS).hostname in ("127.0.0.1", "localhost", "::1"):
        print("❌ ESCRITORIO_WS apunta a esta máquina (¿servidor simulado?); "
              "pon la URL wss:// del escritorio real en .env")
        sys.exit(1)


async def _reset(client: httpx.AsyncClient) -> bool:
    r = await client.post(
        f"{ESCRITORIO_URL}/__reset",
        headers={"Authorization": f"Bearer {ESCRITORIO_TOKEN}"},
    )
    ok = r.status_code == 200
    if not ok:
        print(f"   ⚠️  Reset falló: {r.status_code} {r.text[:200]}")
    return ok


async def _send_task(client: httpx.AsyncClient, instruccion: str) -> str:
    r = await client.post(
        f"{AGENTE_URL}/tasks",
        headers={"Authorization": f"Bearer {EQUIPO_TOKEN}"},
        json={
            "tarea": instruccion,
            "escritorio": {
                "url": ESCRITORIO_WS,
                "password": ESCRITORIO_PASSWORD,
            },
        },
    )
    if r.status_code != 202:
        raise RuntimeError(f"POST /tasks falló: {r.status_code} {r.text}")
    return r.json()["task_id"]


async def _wait_task(client: httpx.AsyncClient, task_id: str) -> dict:
    t0 = time.monotonic()
    while True:
        r = await client.get(
            f"{AGENTE_URL}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {EQUIPO_TOKEN}"},
        )
        data = r.json()
        estado = data["estado"]
        if estado != "en_curso":
            return data
        if time.monotonic() - t0 > 310:  # 5 min + margen
            raise RuntimeError("Timeout esperando la tarea")
        await asyncio.sleep(POLL_INTERVAL)


async def _check(client: httpx.AsyncClient, prueba: dict) -> dict:
    r = await client.post(
        f"{ESCRITORIO_URL}/__check",
        headers={"Authorization": f"Bearer {ESCRITORIO_TOKEN}"},
        json=prueba,
    )
    if r.status_code != 200:
        raise RuntimeError(f"POST /__check falló: {r.status_code} {r.text}")
    return r.json()


def _primer_numero(texto: str) -> str | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", texto)
    return m.group(0).replace(",", ".") if m else None


def evaluar_agente(prueba: dict, estado: str, resumen: str, password: str) -> list[dict]:
    """Chequeos sobre estado y resumen, imitando las reglas del README."""
    checks = [{"nombre": "estado esperado", "ok": estado == prueba.get("estado_esperado")}]
    lineas = [l.strip() for l in resumen.splitlines()
              if l.strip().lower().startswith("respuesta:")]
    esperada = prueba.get("respuesta_esperada")
    if esperada is not None:
        ok = False
        if lineas:
            valor = lineas[0].split(":", 1)[1].strip()
            num = _primer_numero(esperada)
            if num is not None and re.fullmatch(r"-?\d+(?:[.,]\d+)?", esperada.strip()):
                ok = _primer_numero(valor) == num
            else:
                ok = valor.lower() == esperada.strip().lower()
        checks.append({"nombre": "línea respuesta", "ok": ok})
    elif prueba.get("estado_esperado") == "fallida":
        checks.append({"nombre": "sin línea respuesta", "ok": not lineas})
    if prueba.get("claves_razon"):
        checks.append({"nombre": "claves de la razón",
                       "ok": all(k in resumen for k in prueba["claves_razon"])})
    checks.append({"nombre": "sin secretos en resumen",
                   "ok": not (password and password in resumen)})
    return checks


async def main():
    _check_env()
    filtro = {a.upper() for a in sys.argv[1:]}
    archivos = [f for f in sorted(PRUEBAS.glob("*.json"))
                if not filtro or f.name.split("-")[0].upper() in filtro]
    if not archivos:
        print("❌ No hay archivos JSON en pruebas/")
        sys.exit(1)

    resultados: list[dict] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for fichero in archivos:
            print(f"\n{'='*60}")
            print(f"📄 {fichero.name}")
            print("=" * 60)

            with open(fichero) as fh:
                prueba = json.load(fh)

            instruccion = prueba["instruccion"]
            esperado = prueba.get("estado_esperado", "?")
            print(f"📝 Tarea: {instruccion[:80]}...")
            print(f"🎯 Esperado: {esperado}")

            # 1. Reset
            print("🔄 Reset escritorio...")
            if not await _reset(client):
                resultados.append({
                    "prueba": fichero.name,
                    "agente_estado": "N/A",
                    "agente_resumen": "Reset falló",
                    "check_pass": False,
                    "checks": [],
                })
                continue

            # 2. Enviar tarea
            print("🚀 Enviando tarea al agente...")
            task_id = await _send_task(client, instruccion)
            print(f"   task_id: {task_id}")

            # 3. Esperar resultado
            print("⏳ Esperando resultado...")
            resultado_agente = await _wait_task(client, task_id)
            estado_agente = resultado_agente["estado"]
            resumen_agente = resultado_agente.get("resumen", "")
            print(f"   Estado agente: {estado_agente}")
            print(f"   Resumen: {resumen_agente[:200]}...")

            # 4. Check
            print("🔍 Verificando contra /__check...")
            check = await _check(client, prueba)
            checks = check.get("checks", []) + evaluar_agente(
                prueba, estado_agente, resumen_agente, ESCRITORIO_PASSWORD)
            check_pass = check.get("pass", False) and all(c["ok"] for c in checks)
            for c in checks:
                icon = "✅" if c["ok"] else "❌"
                print(f"   {icon} {c['nombre']}: {'OK' if c['ok'] else 'FAIL'}")
            print(f"   Total check: {'PASS' if check_pass else 'FAIL'}")

            resultados.append({
                "prueba": fichero.name,
                "agente_estado": estado_agente,
                "agente_resumen": resumen_agente,
                "check_pass": check_pass,
                "checks": checks,
            })

    # Tabla final
    print("\n" + "=" * 80)
    print(f"{'Prueba':25} {'Estado Agente':15} {'Check':8} {'Detalle'}")
    print("=" * 80)
    for r in resultados:
        ok = "✅ PASS" if r["check_pass"] else "❌ FAIL"
        detalles = ", ".join(
            f"{c['nombre']}: {'OK' if c['ok'] else 'FAIL'}"
            for c in r["checks"]
        )
        print(f"{r['prueba']:25} {r['agente_estado']:15} {ok:8} {detalles}")
    print("=" * 80)

    total = len(resultados)
    passed = sum(1 for r in resultados if r["check_pass"])
    print(f"\n📊 Total: {passed}/{total} pasaron")


if __name__ == "__main__":
    asyncio.run(main())
