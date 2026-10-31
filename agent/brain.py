"""
brain.py — Cerebro del agente.

Expone `decidir(tarea, captura_png, historial)` que consulta un
modelo multimodal (OpenRouter) con la captura de pantalla y devuelve
una lista de acciones JSON para ejecutar.
"""

import base64
import json
import os
import re
from typing import Any

import httpx


# ------------------------------------------------------------------
# Prompt del sistema — reglas duras del reto
# ------------------------------------------------------------------
_SYSTEM_PROMPT = """Eres el cerebro de un agente que opera una aplicación web antigua (SIGECOM) a través de un escritorio remoto de 1280×800 píxeles.

REGLAS ABSOLUTAS:
1. La pantalla es de 1280×800. Todas las coordenadas (x, y) deben estar dentro de esos límites.
2. Puedes devolver VARIAS acciones en un solo turno: clics, tipeos, scrolls, teclas. El bucle las ejecutará secuencialmente.
3. Antes de crear un registro (cliente, pedido, factura…) busca si ya existe para evitar duplicados.
4. Cuando modifiques algo, haz clic en Guardar y verifica visualmente que el cambio quedó reflejado.
5. NO toques nada que esté fuera de la tarea asignada. Cualquier cambio extra cuenta como fallo.
6. "Hoy" es la fecha que muestre la propia aplicación en pantalla; "ayer" o "el sábado" se calculan desde esa fecha.
7. Si la tarea es imposible, ambigua o peligrosa (por ejemplo: dos personas con el mismo nombre, datos insuficientes, etc.) NO modifiques nada en la app. Termina inmediatamente como fallida y nombra en el resumen los datos concretos que generan la ambigüedad (NITs, IDs, nombres completos).
8. En tareas de pregunta ("¿Cuántas…?", "¿Cuál es…?"), el resumen DEBE incluir exactamente una línea:
   respuesta: <valor>
   Si pide dos datos: respuesta: <dato1>; <dato2>
   Si la pregunta no se puede responder, termina como fallida y NO incluyas línea de respuesta.
9. NUNCA repitas la contraseña del escritorio, tokens ni otros secretos en el resumen.
10. Al escribir en un campo que ya tiene texto, usa Ctrl+A primero para seleccionar todo (la capa de ejecución lo hace automáticamente si le pasas select_all=True en type).
11. done DEBE ir SIEMPRE SOLO en su turno, nunca mezclado con otras acciones. Antes de enviar done debes verificar en la pantalla actual que el resultado existe (campo creado/modificado, valor visible, mensaje de confirmación). NUNCA pongas "pendiente de verificar" en el resumen de una tarea terminada; si no estás seguro, NO envíes done, envía wait y espera una captura nueva.

FORMATO DE RESPUESTA — SOLO un array JSON, sin texto adicional, sin markdown:
[
  {"action": "click", "x": 640, "y": 400},
  {"action": "type", "text": "Papelería Los Andes", "select_all": true},
  {"action": "key", "key": "Enter"},
  {"action": "scroll", "x": 640, "y": 400, "dy": -300},
  {"action": "wait"},
  {"action": "done", "estado": "terminada", "resumen": "Se registró el cliente Papelería Los Andes con NIT 901234567. respuesta: 45"}
]

Acciones válidas:
- click      → {action:"click", x:int, y:int}
- double_click → {action:"double_click", x:int, y:int}
- type       → {action:"type", text:string, select_all:bool}
- key        → {action:"key", key:string}  (Enter, Tab, Escape, o combos como "ctrl+a", "shift+Tab")
- scroll     → {action:"scroll", x:int, y:int, dy:int}  (x,y = sobre la lista/panel a desplazar; dy<0 sube, dy>0 baja, 120 = un paso de rueda)
- wait       → {action:"wait"}
- done       → {action:"done", estado:"terminada"|"fallida", resumen:string}

Cuando termines la tarea, la ÚLTIMA acción del array DEBE ser "done" y DEBE ir SOLA, sin otras acciones antes ni después.
"""


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELO_DEFAULT = "anthropic/claude-haiku-5.5"


def _load_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("Falta OPENROUTER_API_KEY en el entorno")
    return key


def _model_name() -> str:
    return os.getenv("MODELO") or MODELO_DEFAULT


def _parse_actions(text: str) -> list[dict[str, Any]]:
    """Extrae el array JSON de la respuesta del modelo, tolerando ```json."""
    text = text.strip()

    # Quitar fences markdown
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Buscar el primer '[' y el último ']'
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return json.loads(text)


async def decidir(
    tarea: str,
    captura_png: bytes,
    historial: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Consulta el modelo multimodal y devuelve la lista de acciones.

    Parámetros
    ----------
    tarea : str
        Instrucción en lenguaje natural.
    captura_png : bytes
        Imagen actual del escritorio en PNG.
    historial : list
        Acciones ya ejecutadas con sus resultados (para contexto).

    Retorna
    -------
    list[dict]
        Cada dict tiene al menos la clave "action".
    """
    api_key = _load_api_key()
    model = _model_name()

    img_b64 = base64.b64encode(captura_png).decode("ascii")

    user_text = (
        f"TAREA: {tarea}\n\n"
        f"HISTORIAL DE ACCIONES YA EJECUTADAS:\n"
        f"{json.dumps(historial, ensure_ascii=False, indent=2)}\n\n"
        f"Describe qué ves en la pantalla y devuelve SOLO el array JSON de acciones."
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                },
            ],
        },
    ]

    async with httpx.AsyncClient(timeout=90.0) as client:
        for attempt in range(2):
            resp = await client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 2048,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]

            try:
                return _parse_actions(raw)
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                if attempt == 0:
                    # Reintento: pedimos explícitamente JSON limpio
                    messages.append({"role": "assistant", "content": raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Eso no es un JSON válido. "
                                "Responde ÚNICAMENTE con un array JSON de acciones, "
                                "sin texto adicional ni markdown."
                            ),
                        }
                    )
                else:
                    raise RuntimeError(
                        f"El modelo no devolvió JSON válido tras reintento: {exc}\n{raw[:500]}"
                    ) from exc

    # Nunca debería llegar aquí
    raise RuntimeError("No se pudo obtener acciones válidas")
