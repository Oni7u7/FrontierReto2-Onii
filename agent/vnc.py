"""Cliente VNC (RFB 3.8) propio sobre WebSocket (websockify).

Sin navegador ni herramientas externas: solo websockets + DES (pycryptodome)
para la autenticacion VNC + Pillow para guardar la captura como PNG.
"""
import asyncio
import io
import struct

import websockets
from Crypto.Cipher import DES
from PIL import Image

# --- tiempos que exige el escritorio ---
CLICK_HOLD = 0.13      # >100 ms entre bajar y soltar, o el clic se ignora
SETTLE = 1.3           # la pantalla tarda hasta 1 s en repintar

KEYSYMS = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "backspace": 0xFF08,
    "escape": 0xFF1B, "esc": 0xFF1B, "delete": 0xFFFF, "home": 0xFF50,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pageup": 0xFF55, "pagedown": 0xFF56, "end": 0xFF57, "space": 0x20,
    "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9,
}
for _i in range(1, 13):
    KEYSYMS[f"f{_i}"] = 0xFFBD + _i


def char_keysym(ch: str) -> int:
    cp = ord(ch)
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    if cp < 0x100:          # ASCII y Latin-1 (incluye ñ, tildes)
        return cp
    return 0x01000000 + cp  # Unicode general


def _vnc_des_key(password: str) -> bytes:
    key = password.encode("latin-1", "ignore")[:8].ljust(8, b"\0")
    # VNC invierte el orden de los bits de cada byte de la clave
    return bytes(int(f"{b:08b}"[::-1], 2) for b in key)


class RFBError(Exception):
    pass


class Desktop:
    def __init__(self, url: str, password: str):
        self.url = url
        self._password = password
        self.ws = None
        self._buf = bytearray()
        self.width = 0
        self.height = 0
        self._fb = None          # bytearray BGRX
        self._mask = 0
        self._x = 0
        self._y = 0

    # ---------- bajo nivel ----------
    async def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            msg = await self.ws.recv()
            if isinstance(msg, str):
                msg = msg.encode("latin-1")
            self._buf += msg
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _send(self, data: bytes):
        await self.ws.send(data)

    # ---------- conexion ----------
    async def connect(self, timeout: float = 20):
        self.ws = await asyncio.wait_for(
            websockets.connect(self.url, subprotocols=["binary"],
                               max_size=None, open_timeout=timeout),
            timeout + 5,
        )
        await asyncio.wait_for(self._handshake(), timeout)

    async def _handshake(self):
        ver = await self._read(12)
        if not ver.startswith(b"RFB "):
            raise RFBError(f"No es un servidor RFB: {ver!r}")
        minor = int(ver[8:11])
        if minor >= 8:
            await self._send(b"RFB 003.008\n")
        elif minor == 7:
            await self._send(b"RFB 003.007\n")
        else:
            await self._send(b"RFB 003.003\n")

        # seguridad
        if minor >= 7:
            n = (await self._read(1))[0]
            if n == 0:
                ln = struct.unpack(">I", await self._read(4))[0]
                raise RFBError("Servidor rechaza: " + (await self._read(ln)).decode("latin-1"))
            types = list(await self._read(n))
            if 2 in types:
                sec = 2
            elif 1 in types:
                sec = 1
            else:
                raise RFBError(f"Seguridad no soportada: {types}")
            await self._send(bytes([sec]))
        else:
            sec = struct.unpack(">I", await self._read(4))[0]
            if sec == 0:
                raise RFBError("Conexion rechazada por el servidor")

        if sec == 2:
            challenge = await self._read(16)
            resp = DES.new(_vnc_des_key(self._password), DES.MODE_ECB).encrypt(challenge)
            await self._send(resp)
        if sec == 2 or minor >= 8:
            result = struct.unpack(">I", await self._read(4))[0]
            if result != 0:
                reason = ""
                if minor >= 8:
                    ln = struct.unpack(">I", await self._read(4))[0]
                    reason = (await self._read(ln)).decode("latin-1")
                raise RFBError("Autenticacion fallida (clave incorrecta?) " + reason)

        await self._send(b"\x01")  # ClientInit: shared
        w, h = struct.unpack(">HH", await self._read(4))
        await self._read(16)  # pixel format del servidor (lo reemplazamos)
        ln = struct.unpack(">I", await self._read(4))[0]
        await self._read(ln)  # nombre del escritorio
        self.width, self.height = w, h
        self._fb = bytearray(w * h * 4)

        # 32 bpp, color verdadero, little-endian: bytes B,G,R,X
        await self._send(
            struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        )
        # solo codificacion Raw (0): la mas simple
        await self._send(struct.pack(">BxHi", 2, 1, 0))

    async def close(self):
        try:
            if self.ws:
                await self.ws.close()
        finally:
            self.ws = None

    # ---------- captura ----------
    async def _request_update(self, incremental: bool = False):
        await self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0,
                                     0, 0, self.width, self.height))

    async def _read_server_message(self) -> bool:
        """Lee un mensaje del servidor. True si fue un FramebufferUpdate."""
        mtype = (await self._read(1))[0]
        if mtype == 0:
            await self._read(1)
            nrects = struct.unpack(">H", await self._read(2))[0]
            for _ in range(nrects):
                x, y, w, h, enc = struct.unpack(">HHHHi", await self._read(12))
                if enc != 0:
                    raise RFBError(f"Codificacion inesperada: {enc}")
                data = await self._read(w * h * 4)
                for row in range(h):
                    dst = ((y + row) * self.width + x) * 4
                    self._fb[dst:dst + w * 4] = data[row * w * 4:(row + 1) * w * 4]
            return True
        if mtype == 1:  # SetColourMapEntries
            await self._read(1)
            _, n = struct.unpack(">HH", await self._read(4))
            await self._read(n * 6)
        elif mtype == 2:  # Bell
            pass
        elif mtype == 3:  # ServerCutText
            await self._read(3)
            ln = struct.unpack(">I", await self._read(4))[0]
            await self._read(ln)
        else:
            raise RFBError(f"Mensaje de servidor desconocido: {mtype}")
        return False

    async def screenshot(self, settle: float = 0.0, timeout: float = 15) -> bytes:
        """Devuelve la pantalla como PNG. `settle` espera antes de capturar."""
        if settle:
            await asyncio.sleep(settle)
        await self._request_update(incremental=False)

        async def _wait():
            while not await self._read_server_message():
                pass
        await asyncio.wait_for(_wait(), timeout)
        img = Image.frombytes("RGB", (self.width, self.height), bytes(self._fb), "raw", "BGRX")
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    # ---------- entrada ----------
    async def _pointer(self, x: int, y: int, mask: int):
        self._x, self._y, self._mask = x, y, mask
        await self._send(struct.pack(">BBHH", 5, mask, x, y))

    async def move(self, x: int, y: int):
        await self._pointer(x, y, 0)

    async def click(self, x: int, y: int, button: int = 1):
        bit = 1 << (button - 1)
        await self._pointer(x, y, 0)
        await asyncio.sleep(0.05)
        await self._pointer(x, y, bit)
        await asyncio.sleep(CLICK_HOLD)
        await self._pointer(x, y, 0)

    async def double_click(self, x: int, y: int):
        await self.click(x, y)
        await asyncio.sleep(0.12)
        await self.click(x, y)

    async def scroll(self, x: int, y: int, dy: int):
        """dy > 0 baja, dy < 0 sube (en pasos de rueda)."""
        bit = 1 << (5 - 1) if dy > 0 else 1 << (4 - 1)
        await self._pointer(x, y, 0)
        for _ in range(abs(dy)):
            await self._pointer(x, y, bit)
            await asyncio.sleep(0.03)
            await self._pointer(x, y, 0)
            await asyncio.sleep(0.03)

    async def _key(self, keysym: int, down: bool):
        await self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    async def press(self, combo: str):
        """'enter', 'tab', 'ctrl+a', 'shift+tab'..."""
        parts = [p.strip().lower() for p in combo.split("+")]
        syms = [KEYSYMS[p] if p in KEYSYMS else char_keysym(p) for p in parts]
        for s in syms:
            await self._key(s, True)
            await asyncio.sleep(0.03)
        for s in reversed(syms):
            await self._key(s, False)
            await asyncio.sleep(0.03)

    async def type_text(self, text: str, select_all: bool = True):
        if select_all:
            await self.press("ctrl+a")  # evita que el texto se concatene
            await asyncio.sleep(0.1)
        for ch in text:
            s = char_keysym(ch)
            await self._key(s, True)
            await asyncio.sleep(0.015)
            await self._key(s, False)
            await asyncio.sleep(0.015)