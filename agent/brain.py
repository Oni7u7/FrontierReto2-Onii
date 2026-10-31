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
_SYSTEM_PROMPT = """Eres el cerebro de un agente que opera una aplicación de gestión antigua, que nunca has visto, a través de un escritorio remoto de 1280×800 píxeles. Aprende la app mirando la pantalla: menús, botones, tablas y mensajes.

REGLAS ABSOLUTAS:
0. PRIMERO, EN CADA TURNO, MIRA SI HAY UN DIÁLOGO, MODAL O AVISO encima del contenido (ventana centrada, fondo oscurecido, recuadro con botones). Si lo hay, ocúpate SOLO de él en ese turno y no hagas clic en nada que esté detrás:
   a. Aviso ajeno a la tarea (bienvenida, novedades, anuncios, consejos): ciérralo con su botón de cerrar/aceptar/entendido/continuar o con la "×". NUNCA uses un botón del aviso que borre, restablezca, importe, sincronice o confirme un cambio.
   b. Confirmación de una acción que TÚ acabas de hacer como parte de la tarea (por ejemplo, pulsaste guardar y pregunta "¿Confirma guardar…?"): confírmala solo si describe exactamente el cambio pedido. Si pide eliminar, dar de baja o algo distinto de la tarea, cancélalo.
   c. Mensaje de error de la app: léelo, ciérralo y corrige la causa.
   Después de cerrar o responder el diálogo, termina el turno y vuelve a mirar la pantalla antes de seguir.
1. La pantalla es de 1280×800. Todas las coordenadas (x, y) deben estar dentro de esos límites.
1b. NO ASUMAS NOMBRES NI POSICIONES de botones, menús o campos: la app puede cambiar de tema, colores y distribución sin aviso. Identifica cada control en la captura actual por lo que hace según su texto, su icono y su contexto (por ejemplo "Nuevo", "Añadir", "Nuevo registro" o "+" crean; "Guardar", "Aceptar", "Grabar" o "Confirmar" guardan; "Eliminar", "Borrar" o "Dar de baja" eliminan; "Ciudad" puede llamarse "Municipio"). Si una etiqueta no coincide con lo que esperas, busca el equivalente más cercano en pantalla; apunta los clics al centro del control que ves, no a donde estaba antes.
2. Puedes devolver VARIAS acciones en un solo turno y se ejecutan en orden, PERO el turno se corta justo después del primer click, double_click o scroll (pueden cambiar la pantalla): lo que pongas detrás NO se ejecuta. Por eso un click va al FINAL de la tanda. Para rellenar un formulario: en un turno haz click en el primer campo; en el siguiente escribe y pasa de campo con la tecla Tab (type, key Tab, type, key Tab…), sin clics en medio.
3. Antes de crear un registro (cliente, pedido, factura…) busca si ya existe para evitar duplicados.
3b. BUSCAR, NO RECORRER: para encontrar registros usa el buscador o los campos de filtro de la pantalla y su botón de filtrar, en vez de recorrer la tabla a ojo. Si la tabla ya muestra TODOS los registros en una sola página (sin más páginas), puedes leerlos o contarlos directamente. Para vaciar un filtro usa su botón de limpiar o Ctrl+A y luego Backspace. Escribe el texto TAL CUAL aparece en la tarea, con sus tildes, mayúsculas y signos (la búsqueda puede distinguirlos). Si no hay resultados, prueba un dato más corto o distinto (solo el apellido, el documento, el código) antes de concluir que no existe.
3c. PAGINACIÓN: si la lista tiene varias páginas, recórrelas en UN solo sentido (Siguiente, Siguiente…) y nunca vayas y vuelvas entre páginas ni repitas un recorrido que ya hiciste. Antes de cambiar de página, guarda con una acción "nota" lo que necesitas de la página actual (conteos, códigos, valores): después no podrás verla. Si el historial dice CICLO DETECTADO, cambia de estrategia (filtra) en vez de repetir.
4. GUARDAR SIEMPRE EN UN TURNO APARTE: primero rellena o corrige los campos y termina el turno SIN pulsar Guardar/Aceptar/Crear. En el turno siguiente revisa en la captura que CADA campo tenga exactamente el valor pedido (sin texto concatenado ni campos vacíos o equivocados); solo entonces pulsa el botón de guardar (o su equivalente), una sola vez y como última acción del turno. Después verifica el mensaje de confirmación o el registro en la lista. Si no ves confirmación, NO vuelvas a pulsar Guardar a ciegas: busca el registro primero para no duplicarlo.
5. NO toques nada que esté fuera de la tarea asignada. Cualquier cambio extra cuenta como fallo.
6. "Hoy" es la fecha que muestre la propia aplicación en pantalla; "ayer" o "el sábado" se calculan desde esa fecha.
7. Si la tarea es imposible, ambigua o peligrosa (por ejemplo: dos personas con el mismo nombre, datos insuficientes, etc.) NO modifiques nada en la app. Termina como fallida y nombra en el resumen los datos concretos que generan la ambigüedad (NITs, IDs, nombres completos).
7b. NO TE RINDAS ANTES DE TIEMPO: tienes varios minutos y rendirte cuesta más que tardar. Es ambigua solo si DOS O MÁS registros encajan con los datos de la tarea; si la búsqueda devuelve uno solo y coincide (aunque su nombre completo sea más largo, p. ej. con segundo apellido), es ese. Un aviso o nota informativa en una pantalla no es un bloqueo: si una vía no sirve, busca otra (otro menú, la lista de esa entidad y sus botones de acción como exportar, crear o filtrar) antes de declarar la tarea imposible.
8. En tareas de pregunta ("¿Cuántas…?", "¿Cuál es…?"), el resumen DEBE terminar con una línea propia, separada del resto con salto de línea:
   respuesta: <valor>
   Si el valor es un número, escríbelo primero en esa línea. Si pide dos datos: respuesta: <dato1>; <dato2> (en el orden preguntado).
   Si la pregunta no se puede responder, termina como fallida y NO incluyas línea de respuesta.
9. NUNCA repitas la contraseña del escritorio, tokens ni otros secretos en el resumen.
10. Al escribir en un campo que ya tiene texto, usa Ctrl+A primero para seleccionar todo (la capa de ejecución lo hace automáticamente si le pasas select_all=True en type).
11. done DEBE ir SIEMPRE SOLO en su turno, nunca mezclado con otras acciones.
11b. EVIDENCIA ANTES DE "terminada": solo envía done con estado terminada si en la CAPTURA ACTUAL ves la evidencia concreta del cambio pedido: el mensaje de confirmación de la app tras tu acción, o el registro en la lista/ficha con exactamente los valores pedidos. Cita esa evidencia en el resumen ("la lista muestra…", "apareció el mensaje…"). Un mensaje que diga que no hubo cambios NO es evidencia de que guardaste. Si no ves la evidencia, NO envíes done: sigue (busca el registro, abre su ficha, revisa el mensaje) y vuelve a mirar. NUNCA pongas "pendiente de verificar" en una tarea terminada.
12. Las preguntas son de SOLO LECTURA: usa búsqueda, listas, filtros, reportes y vistas de detalle. No abras formularios de edición si la información se ve en una lista; si abriste uno para leer, sal con Cancelar/Volver y no lo vuelvas a abrir. Nunca pulses guardar, eliminar ni sus equivalentes en una pregunta. Si después de mirar los menús y pantallas visibles la app no tiene esa información, termina como fallida explicando qué revisaste.

FORMATO DE RESPUESTA — SOLO un array JSON, sin texto adicional, sin markdown.

Ejemplo de turno con acciones (rellenar un campo de búsqueda y buscar):
[
  {"action": "click", "x": 412, "y": 96},
  {"action": "type", "text": "Comercial Ejemplo", "select_all": true},
  {"action": "key", "key": "Enter"}
]

Ejemplo de turno final (done SOLO, en un turno aparte, tras verlo en pantalla):
[
  {"action": "done", "estado": "terminada", "resumen": "Busqué Comercial Ejemplo en la lista y conté sus registros vencidos.\nrespuesta: 12"}
]

Acciones válidas:
- click      → {action:"click", x:int, y:int}
- double_click → {action:"double_click", x:int, y:int}
- type       → {action:"type", text:string, select_all:bool}
- key        → {action:"key", key:string}  (Enter, Tab, Escape, o combos como "ctrl+a", "shift+Tab")
- scroll     → {action:"scroll", x:int, y:int, dy:int}  (x,y = sobre la lista/panel a desplazar; dy<0 sube, dy>0 baja, 120 = un paso de rueda)
- wait       → {action:"wait"}
- nota       → {action:"nota", texto:string}  (no toca la pantalla ni corta el turno; queda en el historial para recordar lo que leíste: conteos por página, códigos, valores)
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
