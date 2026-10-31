"""tests/test_vnc_fixes.py — Un test por cada arreglo de agent/vnc.py.

Servidor RFB falso configurable: tipos de seguridad ofrecidos, tamaño de
pantalla, rects que manda y si los parte en varios mensajes. Registra los
eventos de puntero/teclado con su hora para medir pausas.
"""
import io
import os
import struct
import sys
import time
from pathlib import Path

import pytest
import websockets
from Crypto.Cipher import DES
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import vnc
from agent.vnc import Desktop, _vnc_des_key, char_keysym

PASS = "secreto1"


def _servidor(sec_types=(2,), w=64, h=32, rects=None, un_mensaje=False):
    """Crea un handler RFB falso. `rects` es [(x, y, w, h, bgrx_bytes)];
    por defecto un rect de pantalla completa en gris."""
    eventos = []
    if rects is None:
        rects = [(0, 0, w, h, bytes([90, 90, 90, 0]) * (w * h))]

    async def handler(ws):
        buf = bytearray()

        async def rd(n):
            while len(buf) < n:
                buf.extend(await ws.recv())
            out = bytes(buf[:n])
            del buf[:n]
            return out

        await ws.send(b"RFB 003.008\n")
        await rd(12)
        await ws.send(bytes([len(sec_types), *sec_types]))
        elegido = (await rd(1))[0]
        eventos.append(("sec", elegido))
        if elegido == 2:
            ch = os.urandom(16)
            await ws.send(ch)
            ok = await rd(16) == DES.new(_vnc_des_key(PASS), DES.MODE_ECB).encrypt(ch)
        else:
            ok = True
        await ws.send(struct.pack("!I", 0 if ok else 1))
        if not ok:
            await ws.send(struct.pack("!I", 4) + b"fail")
            return

        await rd(1)  # ClientInit
        await ws.send(struct.pack("!HH", w, h) + bytes(16) + struct.pack("!I", 4) + b"mock")

        while True:
            t = (await rd(1))[0]
            if t == 0:
                await rd(19)
            elif t == 2:
                n = struct.unpack("!H", (await rd(3))[1:])[0]
                await rd(4 * n)
            elif t == 3:
                await rd(9)
                msg = struct.pack("!BxH", 0, len(rects))
                for rx, ry, rw, rh, data in rects:
                    msg += struct.pack("!HHHHi", rx, ry, rw, rh, 0) + data
                if un_mensaje:
                    await ws.send(msg)  # todo en un solo frame websocket
                else:
                    await ws.send(msg[:7])
                    await ws.send(msg[7:])
            elif t == 5:
                b, x, y = struct.unpack("!BHH", await rd(5))
                eventos.append(("ptr", b, x, y, time.monotonic()))
            elif t == 4:
                d, k = struct.unpack("!BxxI", await rd(7))
                eventos.append(("key", d, k))

    return handler, eventos


async def _conectar(handler, password=PASS):
    server = await websockets.serve(
        handler, "127.0.0.1", 0, subprotocols=["binary"], max_size=None
    )
    port = server.sockets[0].getsockname()[1]
    d = Desktop(f"ws://127.0.0.1:{port}", password)
    await d.connect()
    return server, d


async def _cerrar(server, d):
    await d.close()
    server.close()
    await server.wait_closed()


async def _esperar_eventos(eventos, n, timeout=2.0):
    """Los eventos llegan al servidor de forma asíncrona: espera a tener n."""
    import asyncio
    fin = time.monotonic() + timeout
    while len(eventos) < n and time.monotonic() < fin:
        await asyncio.sleep(0.01)


# 1. Clic con CLICK_HOLD entre bajar y soltar --------------------------------

@pytest.mark.asyncio
async def test_click_mantiene_boton_al_menos_100ms():
    handler, ev = _servidor()
    server, d = await _conectar(handler)
    try:
        await d.click(10, 20)
        await _esperar_eventos(ev, 4)  # sec + 3 ptr
        ptr = [e for e in ev if e[0] == "ptr"]
        assert [p[1] for p in ptr] == [0, 1, 0]
        assert all(p[2:4] == (10, 20) for p in ptr)
        hold = ptr[2][4] - ptr[1][4]
        assert vnc.CLICK_HOLD >= 0.13
        assert hold >= 0.1, f"botón sostenido solo {hold * 1000:.0f} ms"
    finally:
        await _cerrar(server, d)


@pytest.mark.asyncio
async def test_double_click_dos_clics_sostenidos_y_rapidos():
    handler, ev = _servidor()
    server, d = await _conectar(handler)
    try:
        await d.double_click(5, 6)
        await _esperar_eventos(ev, 7)
        ptr = [e for e in ev if e[0] == "ptr"]
        assert [p[1] for p in ptr] == [0, 1, 0, 0, 1, 0]
        assert ptr[2][4] - ptr[1][4] >= 0.1
        assert ptr[5][4] - ptr[4][4] >= 0.1
        # el doble clic completo debe caber en la ventana típica de 500 ms
        assert ptr[5][4] - ptr[1][4] < 0.5
    finally:
        await _cerrar(server, d)


# 2. press() con combos -------------------------------------------------------

@pytest.mark.asyncio
async def test_press_combo_baja_en_orden_y_suelta_al_reves():
    handler, ev = _servidor()
    server, d = await _conectar(handler)
    try:
        await d.press("ctrl+a")
        await d.press("Shift+Tab")
        await d.press("Enter")
        await _esperar_eventos(ev, 1 + 10)
        keys = [(e[1], e[2]) for e in ev if e[0] == "key"]
        assert keys == [
            (1, 0xFFE3), (1, ord("a")), (0, ord("a")), (0, 0xFFE3),
            (1, 0xFFE1), (1, 0xFF09), (0, 0xFF09), (0, 0xFFE1),
            (1, 0xFF0D), (0, 0xFF0D),
        ]
    finally:
        await _cerrar(server, d)


def test_keyname_desconocida_lanza_error_en_vez_de_mandar_otra_tecla():
    assert Desktop._keyname_to_keysym("ctrl") == 0xFFE3
    assert Desktop._keyname_to_keysym("PageDown") == 0xFF56
    assert Desktop._keyname_to_keysym("F5") == 0xFFC2
    assert Desktop._keyname_to_keysym("A") == ord("a")  # combos sin Shift implícito
    with pytest.raises(ValueError):
        Desktop._keyname_to_keysym("ctrl+a")  # el combo se parte en press(), no aquí
    with pytest.raises(ValueError):
        Desktop._keyname_to_keysym("Intro")


def test_char_keysym_latin1_y_unicode():
    assert char_keysym("A") == 0x41
    assert char_keysym("í") == 0xED
    assert char_keysym("ñ") == 0xF1
    assert char_keysym("€") == 0x01000000 + 0x20AC
    assert char_keysym("\n") == 0xFF0D


# 3. scroll(x, y, dy) ---------------------------------------------------------

@pytest.mark.asyncio
async def test_scroll_sobre_xy_con_mascara_de_rueda():
    handler, ev = _servidor()
    server, d = await _conectar(handler)
    try:
        await d.scroll(300, 400, 240)    # 2 pasos abajo
        await d.scroll(50, 60, -100)     # 1 paso arriba (mínimo)
        await d.scroll(50, 60, 0)        # nada
        await _esperar_eventos(ev, 1 + 5 + 3)
        ptr = [e[1:4] for e in ev if e[0] == "ptr"]
        assert ptr == [
            (0, 300, 400), (16, 300, 400), (0, 300, 400), (16, 300, 400), (0, 300, 400),
            (0, 50, 60), (8, 50, 60), (0, 50, 60),
        ]
        # nunca debe salir el bit 4 (=botón 3, clic derecho)
        assert all(b & 4 == 0 for b, _, _ in ptr)
    finally:
        await _cerrar(server, d)


# 4. max_size=None: frame de pantalla completa (>1 MB) en un solo mensaje ----

@pytest.mark.asyncio
async def test_screenshot_1280x800_en_un_solo_frame_websocket():
    w, h = 1280, 800
    handler, _ = _servidor(w=w, h=h, un_mensaje=True)
    server, d = await _conectar(handler)
    try:
        img = Image.open(io.BytesIO(await d.screenshot()))
        assert img.size == (1280, 800)
        assert img.getpixel((640, 400)) == (90, 90, 90)
    finally:
        await _cerrar(server, d)


# 5. Tipos de seguridad 1 y 2 -------------------------------------------------

@pytest.mark.asyncio
async def test_security_none_si_no_hay_vnc_auth():
    handler, ev = _servidor(sec_types=(1,))
    server, d = await _conectar(handler, password="")
    try:
        assert ("sec", 1) in ev
        assert (d.width, d.height) == (64, 32)
    finally:
        await _cerrar(server, d)


@pytest.mark.asyncio
async def test_security_prefiere_vnc_auth_si_ofrecen_ambos():
    handler, ev = _servidor(sec_types=(1, 2))
    server, d = await _conectar(handler)
    try:
        assert ("sec", 2) in ev
    finally:
        await _cerrar(server, d)


@pytest.mark.asyncio
async def test_security_no_soportado_falla_con_mensaje_claro():
    handler, _ = _servidor(sec_types=(16, 19))
    server = await websockets.serve(handler, "127.0.0.1", 0, subprotocols=["binary"])
    port = server.sockets[0].getsockname()[1]
    d = Desktop(f"ws://127.0.0.1:{port}", PASS)
    try:
        with pytest.raises(Exception, match="no soportados"):
            await d.connect()
    finally:
        await d.close()
        server.close()
        await server.wait_closed()


# 6. screenshot con Image.frombytes BGRX: colores y posición de rects --------

@pytest.mark.asyncio
async def test_screenshot_bgrx_colores_y_rects_parciales():
    w, h = 40, 20
    fondo = bytes([0, 0, 0, 0]) * (w * h)                 # negro
    rojo = bytes([0, 0, 255, 0]) * (10 * 5)               # BGRX → rojo
    azul = bytes([255, 0, 0, 0]) * (4 * 3)                # BGRX → azul
    handler, _ = _servidor(
        w=w, h=h,
        rects=[(0, 0, w, h, fondo), (5, 2, 10, 5, rojo), (30, 15, 4, 3, azul)],
    )
    server, d = await _conectar(handler)
    try:
        img = Image.open(io.BytesIO(await d.screenshot()))
        assert img.getpixel((0, 0)) == (0, 0, 0)
        assert img.getpixel((5, 2)) == (255, 0, 0)
        assert img.getpixel((14, 6)) == (255, 0, 0)
        assert img.getpixel((15, 6)) == (0, 0, 0)       # justo fuera del rect
        assert img.getpixel((31, 16)) == (0, 0, 255)
    finally:
        await _cerrar(server, d)
