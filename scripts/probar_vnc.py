"""
scripts/probar_vnc.py — Script de prueba para validar la capa VNC.

Uso:
    1. Copia .env.example a .env y rellena ESCRITORIO_URL y ESCRITORIO_PASSWORD.
    2. python scripts/probar_vnc.py
    3. Revisa captura.png y despues.png (deben ser 1280×800 y mostrar SIGECOM).
"""

import asyncio
import os
import sys
from pathlib import Path

# Añadimos la raíz del proyecto al PYTHONPATH
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from agent.vnc import VNCController

load_dotenv(ROOT / ".env")


async def main():
    url = os.getenv("ESCRITORIO_URL")
    password = os.getenv("ESCRITORIO_PASSWORD")

    if not url or not password:
        print("❌ Faltan variables en .env: ESCRITORIO_URL o ESCRITORIO_PASSWORD")
        sys.exit(1)

    vnc = VNCController(url=url, password=password)

    try:
        print("🔌 Conectando a noVNC...")
        await vnc.connect(headless=True)
        print("✅ Conectado. Capturando pantalla inicial...")

        img_before = await vnc.screenshot()
        path_before = ROOT / "captura.png"
        with open(path_before, "wb") as f:
            f.write(img_before)
        print(f"📸 Guardada: {path_before} ({len(img_before)} bytes)")

        # Clic de prueba en el centro (640, 400)
        print("🖱️  Clic de prueba en centro de pantalla (640, 400)...")
        await vnc.click(640, 400)

        print("📸 Capturando después del clic...")
        img_after = await vnc.screenshot()
        path_after = ROOT / "despues.png"
        with open(path_after, "wb") as f:
            f.write(img_after)
        print(f"📸 Guardada: {path_after} ({len(img_after)} bytes)")

    except Exception as exc:
        print(f"💥 Error: {exc}")
        raise

    finally:
        await vnc.disconnect()
        print("🔒 Desconectado.")


if __name__ == "__main__":
    asyncio.run(main())
