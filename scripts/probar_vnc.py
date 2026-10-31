"""scripts/probar_vnc.py — Prueba real de agent/vnc.py contra el escritorio.

Paso 1 (siempre, solo lectura): conecta con ESCRITORIO_WS / ESCRITORIO_PASSWORD
del .env, guarda captura.png, verifica 1280x800 y que la imagen tenga colores
reales, y mide la latencia de captura.

Paso 2 (solo con --campo X Y): clic en ese campo de texto, mide cuánto tarda en
repintar, escribe un texto con mayúsculas y tildes y guarda captura_despues.png
para comparar a ojo. Elige X Y mirando captura.png: un campo de búsqueda o de
texto, nunca un botón.

Uso:
    python scripts/probar_vnc.py
    python scripts/probar_vnc.py --campo 400 120 --reset
"""
import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# override=True: el .env manda. Sin él, una ESCRITORIO_WS vieja exportada en el
# shell (p. ej. ws://127.0.0.1:8765 del servidor simulado) gana en silencio.
load_dotenv(ROOT / ".env", override=True)

from agent.vnc import Desktop  # noqa: E402
from scripts.runner import base_https  # noqa: E402

TEXTO_PRUEBA = "Ñandú Pérez ÁRBOL 123"


def _img(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _revisar_colores(img: Image.Image) -> bool:
    """Una captura sana tiene muchos colores; una con el formato de píxel mal
    leído suele salir negra, de un solo color o con ruido."""
    colores = img.getcolors(maxcolors=1 << 16)
    n = len(colores) if colores else 1 << 16
    dominantes = sorted(colores or [], reverse=True)[:5]
    print(f"   colores distintos: {n if n < 1 << 16 else '>65536'}")
    print("   dominantes (cuenta, RGB):", ", ".join(f"{c}×{rgb}" for c, rgb in dominantes))
    if n < 4:
        print("❌ La imagen es casi de un solo color: formato de píxel o servidor mal")
        return False
    print("   👀 Abre captura.png: si el azul se ve naranja (o al revés), R y B están invertidos")
    return True


def _pixeles_distintos(a: Image.Image, b: Image.Image) -> int:
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 16 else 0)
    return diff.histogram()[255]


def _escritorio_ws() -> str:
    """ESCRITORIO_WS obligatoria, remota y con esquema ws/wss; si no, sale con
    un mensaje claro. Nunca imprime la URL."""
    ws_url = (os.getenv("ESCRITORIO_WS") or "").strip()
    if not ws_url:
        print("❌ Falta ESCRITORIO_WS en .env (wss://.../websockify)")
        sys.exit(1)
    partes = urlsplit(ws_url)
    if partes.scheme not in ("ws", "wss") or not partes.hostname:
        print("❌ ESCRITORIO_WS no es una URL ws:// o wss:// válida")
        sys.exit(1)
    if partes.hostname in ("127.0.0.1", "localhost", "::1"):
        print("❌ ESCRITORIO_WS apunta a esta máquina (¿servidor simulado?); "
              "pon la URL wss:// del escritorio real en .env")
        sys.exit(1)
    return ws_url


async def _reset(ws_url: str):
    import httpx
    url = base_https(ws_url)
    token = os.getenv("ESCRITORIO_TOKEN", "")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{url}/__reset", headers={"Authorization": f"Bearer {token}"})
    print(f"🔄 /__reset → {r.status_code}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campo", nargs=2, type=int, metavar=("X", "Y"),
                    help="coordenadas de un campo de texto para probar clic y escritura")
    ap.add_argument("--reset", action="store_true",
                    help="llama /__reset al final para dejar el escritorio limpio")
    args = ap.parse_args()

    ws_url = _escritorio_ws()
    password = os.getenv("ESCRITORIO_PASSWORD")
    if password is None:
        print("❌ Falta ESCRITORIO_PASSWORD en .env")
        sys.exit(1)

    ok = True
    d = Desktop(ws_url, password)
    print("🔌 Conectando (la URL y la contraseña no se imprimen)...")
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(d.connect(), timeout=15)
    except Exception as exc:
        print(f"❌ Conexión fallida: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print(f"✅ Conectado en {time.monotonic() - t0:.2f}s — pantalla {d.width}x{d.height}")

    try:
        # ---- Paso 1: captura (solo lectura) ----
        tiempos = []
        for _ in range(3):
            t = time.monotonic()
            png = await d.screenshot()
            tiempos.append(time.monotonic() - t)
        print("⏱️  captura completa: " + ", ".join(f"{x:.2f}s" for x in tiempos)
              + f" ({len(png) / 1024:.0f} KB PNG)")
        antes = _img(png)
        (ROOT / "captura.png").write_bytes(png)
        print(f"💾 {ROOT / 'captura.png'}")

        if antes.size != (1280, 800):
            print(f"❌ Tamaño {antes.size}, se esperaba (1280, 800)")
            ok = False
        else:
            print("✅ Tamaño 1280x800")
        ok &= _revisar_colores(antes)

        # ---- Paso 2: clic + escritura (opcional) ----
        if args.campo:
            x, y = args.campo
            print(f"🖱️  Clic en ({x}, {y})...")
            t = time.monotonic()
            await d.click(x, y)
            repinto = None
            while time.monotonic() - t < 2.5:
                ahora = _img(await d.screenshot())
                if _pixeles_distintos(antes, ahora) > 0:
                    repinto = time.monotonic() - t
                    break
            if repinto is None:
                print("⚠️  La pantalla no cambió en 2.5 s tras el clic (¿clic ignorado o sin efecto visible?)")
            else:
                print(f"✅ La pantalla cambió {repinto:.2f}s después del clic")

            print(f"⌨️  Escribiendo {TEXTO_PRUEBA!r} con Ctrl+A previo...")
            await d.type_text(TEXTO_PRUEBA, select_all=True)
            despues = await d.screenshot(settle=1.3)
            (ROOT / "captura_despues.png").write_bytes(despues)
            n = _pixeles_distintos(antes, _img(despues))
            print(f"💾 {ROOT / 'captura_despues.png'} ({n} píxeles cambiaron)")
            print("   👀 Revisa que el campo diga exactamente el texto, con mayúsculas y tildes")
    finally:
        await d.close()

    if args.reset:
        await _reset(ws_url)

    print("\n" + ("✅ Prueba básica OK" if ok else "❌ Hay problemas: revisa las capturas"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
