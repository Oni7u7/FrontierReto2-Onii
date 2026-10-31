"""scripts/probar_brain.py — Prueba la API real de OpenRouter con una imagen sintética."""
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.brain import MODELO_DEFAULT, decidir

load_dotenv(ROOT / ".env")


def crear_imagen_formulario() -> bytes:
    """Genera una imagen 1280×800 que simula un formulario de SIGECOM."""
    img = Image.new("RGB", (1280, 800), "#f5f5f5")
    draw = ImageDraw.Draw(img)

    # Fondo de encabezado
    draw.rectangle([0, 0, 1280, 60], fill="#2c3e50")
    draw.text((20, 18), "SIGECOM — Sistema de Gestión Comercial", fill="white", font=None)

    # Título
    draw.text((100, 100), "Nuevo Cliente", fill="#333333", font=None)

    # Campo Nombre
    draw.rectangle([100, 180, 600, 220], fill="white", outline="#999999", width=2)
    draw.text((110, 156), "Nombre:", fill="#333333", font=None)

    # Campo NIT
    draw.rectangle([100, 300, 600, 340], fill="white", outline="#999999", width=2)
    draw.text((110, 276), "NIT:", fill="#333333", font=None)

    # Botón Guardar
    draw.rectangle([100, 450, 300, 490], fill="#27ae60", outline="#1e8449", width=2)
    draw.text((140, 456), "Guardar", fill="white", font=None)

    buf = bytearray()
    from io import BytesIO
    bio = BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


async def main():
    modelo = os.getenv("MODELO") or MODELO_DEFAULT
    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key or api_key == "sk-or-v1-...":
        print("❌ Faltan OPENROUTER_API_KEY en .env (no es 'sk-or-v1-...')")
        sys.exit(1)

    print(f" Generando imagen de prueba (1280×800)...")
    imagen = crear_imagen_formulario()
    print(f"   Imagen: {len(imagen)} bytes")

    tarea = "Escribe 'Prueba' en el campo Nombre y luego haz clic en Guardar."
    print(f"🧠 Consultando modelo: {modelo}")
    print(f"📝 Tarea: {tarea}")

    try:
        t0 = time.monotonic()
        acciones = await decidir(tarea, imagen, [])
        dt = time.monotonic() - t0
        print(f"\n⏱️  Tiempo de respuesta del modelo: {dt:.1f}s")
        print(f"✅ Acciones recibidas ({len(acciones)}):")
        for i, a in enumerate(acciones, 1):
            print(f"   {i}. {a}")
    except Exception as exc:
        print(f"\n💥 Error: {exc}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
