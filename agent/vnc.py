"""
vnc.py — Cliente RFB 3.8 propio sobre WebSocket (sin Playwright).

Conecta directamente al endpoint /websockify del escritorio,
autentica con DES-VNC, pide framebuffer y expone operaciones
de bajo nivel: screenshot, click, double_click, type_text,
press, scroll.

Las pausas post-acción NO van aquí; el bucle llama
screenshot(settle=...) cuando necesita estabilizar.
"""

import asyncio
import io
import struct
from typing import Optional

import websockets
from PIL import Image
from Crypto.Cipher import DES

# El escritorio ignora clics con menos de 100 ms entre bajar y soltar.
CLICK_HOLD = 0.13
# Pausa entre los dos clics de un doble clic (debe quedar bajo ~500 ms en total).
DOUBLE_CLICK_GAP = 0.06

# Nombres de tecla (en minúsculas) → keysym X11.
KEYSYMS = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "tab": 0xFF09,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "del": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "control": 0xFFE3,
    "ctrl": 0xFFE3,
    "alt": 0xFFE9,
    "shift": 0xFFE1,
    "super": 0xFFEB,
    "meta": 0xFFEB,
    "space": 0x20,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}


def char_keysym(ch: str) -> int:
    """Keysym de un carácter: Latin-1 coincide con el code point; el resto
    de Unicode usa 0x01000000 + code point (convención X11)."""
    if ch == "\n":
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    cp = ord(ch)
    return cp if cp <= 0xFF else 0x01000000 + cp


def _vnc_des_key(password: str) -> bytes:
    """Deriva la clave DES de 8 bytes que exige el auth VNC.

    La contraseña se trunca/rellena a 8 bytes y luego se
    invierten los bits de cada byte (bit 0 ↔ bit 7, etc.).
    """
    pw = password.encode("latin-1")[:8]
    pw = pw + b"\x00" * (8 - len(pw))
    return bytes(
        sum(((byte >> i) & 1) << (7 - i) for i in range(8))
        for byte in pw
    )


class Desktop:
    """Controlador de escritorio remoto vía RFB sobre WebSocket."""

    def __init__(self, url: str, password: str):
        self.url = url
        self.password = password
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.width: int = 0
        self.height: int = 0
        self._connected: bool = False
        self._buf = bytearray()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Handshake RFB 3.8 + VNC auth + inicialización."""
        # max_size=None: un frame de pantalla completa puede pasar de 1 MB.
        self.ws = await websockets.connect(
            self.url, subprotocols=["binary"], max_size=None
        )

        # 1. Versión
        await self._send(b"RFB 003.008\n")
        assert await self._recv_exact(12) == b"RFB 003.008\n", "version mismatch"

        # 2. Security types
        num_types = (await self._recv_exact(1))[0]
        if num_types == 0:
            raise Exception("No security types offered")
        types = await self._recv_exact(num_types)
        if 2 in types:
            await self._send(b"\x02")  # VNC auth (contraseña)

            # 3. VNC authentication
            challenge = await self._recv_exact(16)
            key = _vnc_des_key(self.password)
            cipher = DES.new(key, DES.MODE_ECB)
            response = cipher.encrypt(challenge)
            await self._send(response)
        elif 1 in types:
            await self._send(b"\x01")  # None: sin contraseña
        else:
            raise Exception(f"Tipos de seguridad no soportados: {list(types)}")

        # 4. Auth result (big-endian según RFB; en 3.8 también llega con None)
        result = struct.unpack("!I", await self._recv_exact(4))[0]
        if result != 0:
            reason_len = struct.unpack("!I", await self._recv_exact(4))[0]
            reason = (await self._recv_exact(reason_len)).decode("latin-1", errors="replace")
            raise Exception(f"Autenticacion fallida: {reason}")

        # 5. ClientInit (shared = 1)
        await self._send(b"\x01")

        # 6. ServerInit (big-endian)
        self.width = struct.unpack("!H", await self._recv_exact(2))[0]
        self.height = struct.unpack("!H", await self._recv_exact(2))[0]
        _pix_format = await self._recv_exact(16)
        name_len = struct.unpack("!I", await self._recv_exact(4))[0]
        await self._recv_exact(name_len)

        # 7. SetPixelFormat → pedimos 32-bit BGRX
        #    RFB: type(1) + pad(3) + format(16) = 20 bytes
        pf = struct.pack(
            "!BBBBHHHBBBxxx",
            32,   # bits-per-pixel
            24,   # depth
            0,    # big-endian flag
            1,    # true-color
            255, 255, 255,   # red, green, blue max
            16, 8, 0,        # red, green, blue shift
        )
        await self._send(b"\x00\x00\x00\x00" + pf)

        # 8. SetEncodings → solo Raw (0)
        #    RFB: type(1) + pad(1) + count(2) + encodings(4*N)
        await self._send(struct.pack("!BBHi", 2, 0, 1, 0))

        self._connected = True

    async def close(self) -> None:
        """Cierra el WebSocket."""
        if self.ws:
            await self.ws.close()
            self.ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # Operaciones públicas (sin pausas internas)
    # ------------------------------------------------------------------

    async def screenshot(self, settle: float = 0.0) -> bytes:
        """Pide un FramebufferUpdate completo y lo devuelve como PNG.

        Si settle > 0, espera ese tiempo antes de pedir la imagen.
        """
        if settle > 0:
            await asyncio.sleep(settle)

        if not self._connected:
            raise RuntimeError("No conectado")

        # FramebufferUpdateRequest: incremental=0, full screen
        req = struct.pack("!BBHHHH", 3, 0, 0, 0, self.width, self.height)
        await self._send(req)

        # Leer hasta recibir FramebufferUpdate (type 0), saltando Bell, etc.
        while True:
            msg_type, payload = await self._read_message()
            if msg_type == 0:
                rects = payload
                break

        # Construir imagen RGB: Pillow decodifica BGRX crudo directamente
        img = Image.new("RGB", (self.width, self.height))
        for x, y, w, h, _enc, pixels in rects:
            if w == 0 or h == 0:
                continue
            img.paste(Image.frombytes("RGB", (w, h), pixels, "raw", "BGRX"), (x, y))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def click(self, x: int, y: int) -> None:
        """Clic izquierdo: move + down + pausa CLICK_HOLD + up."""
        await self._pointer(0, x, y)   # move (sin botón)
        await self._pointer(1, x, y)   # botón izquierdo presionado
        await asyncio.sleep(CLICK_HOLD)
        await self._pointer(0, x, y)   # botón soltado

    async def double_click(self, x: int, y: int) -> None:
        """Doble clic: dos clics con una pausa corta entre ellos."""
        await self.click(x, y)
        await asyncio.sleep(DOUBLE_CLICK_GAP)
        await self.click(x, y)

    async def type_text(self, text: str, select_all: bool = True) -> None:
        """Escribe texto; si select_all=True envía Ctrl+A primero."""
        if select_all:
            await self._key(0xFFE3, True)   # Ctrl  down
            await self._key(0x61, True)     # a     down
            await self._key(0x61, False)    # a     up
            await self._key(0xFFE3, False)  # Ctrl  up

        for ch in text:
            sym = char_keysym(ch)
            await self._key(sym, True)
            await self._key(sym, False)

    async def press(self, key: str) -> None:
        """Presiona una tecla o combo ("Enter", "ctrl+a", "shift+Tab").

        Baja las partes en orden y las suelta en orden inverso.
        """
        syms = [self._keyname_to_keysym(part) for part in key.split("+")]
        for sym in syms:
            await self._key(sym, True)
        for sym in reversed(syms):
            await self._key(sym, False)

    async def scroll(self, x: int, y: int, dy: int) -> None:
        """Rueda del ratón sobre (x, y): dy<0 sube (botón 4), dy>0 baja (5).

        La rueda actúa sobre el elemento bajo el puntero, por eso se mueve
        primero a (x, y). Cada 120 de dy es un paso de rueda (mínimo 1).
        """
        if dy == 0:
            return
        mask = 8 if dy < 0 else 16   # bit 3 = botón 4, bit 4 = botón 5
        steps = max(1, abs(dy) // 120)
        await self._pointer(0, x, y)
        for _ in range(steps):
            await self._pointer(mask, x, y)
            await self._pointer(0, x, y)

    # ------------------------------------------------------------------
    # Helpers de red / protocolo
    # ------------------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        await self.ws.send(data)

    async def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self.ws.recv()
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _read_message(self):
        """Lee un mensaje RFB y devuelve (type, payload).

        Payload:
          * type 0 → lista de rects [(x,y,w,h,enc,bytes), ...]
          * type 2 → b''   (Bell, ignorado)
          * type 3 → bytes (ServerCutText)
        """
        msg_type = (await self._recv_exact(1))[0]

        if msg_type == 0:  # FramebufferUpdate
            await self._recv_exact(1)                 # padding
            num_rects = struct.unpack("!H", await self._recv_exact(2))[0]
            rects = []
            for _ in range(num_rects):
                x, y, w, h, enc = struct.unpack(
                    "!HHHHi", await self._recv_exact(12))
                if enc != 0:
                    raise Exception(f"Encoding {enc} no soportado")
                data_len = w * h * 4  # 32 bpp = 4 bytes/pixel
                data = await self._recv_exact(data_len)
                rects.append((x, y, w, h, enc, data))
            return 0, rects

        if msg_type == 2:  # Bell
            return 2, b""

        if msg_type == 3:  # ServerCutText
            await self._recv_exact(3)  # padding
            length = struct.unpack("!I", await self._recv_exact(4))[0]
            return 3, await self._recv_exact(length)

        # Otros tipos no esperados en este reto
        return msg_type, b""

    async def _pointer(self, button: int, x: int, y: int) -> None:
        """PointerEvent RFB: 1 byte botón + 2 x + 2 y."""
        await self._send(struct.pack("!BBHH", 5, button, x, y))

    async def _key(self, keysym: int, down: bool) -> None:
        """KeyEvent RFB: 1 tipo + 1 flag + 2 pad + 4 keysym."""
        flag = 1 if down else 0
        await self._send(struct.pack("!BBxxI", 4, flag, keysym))

    # ------------------------------------------------------------------
    # Mapeo de caracteres / teclas
    # ------------------------------------------------------------------

    @staticmethod
    def _keyname_to_keysym(key: str) -> int:
        """Nombre de tecla ("Enter", "ctrl") o carácter suelto ("a") → keysym."""
        name = key.strip()
        sym = KEYSYMS.get(name.lower().replace("_", ""))
        if sym is not None:
            return sym
        if len(name) == 1:
            # Combos con letra: usar minúscula para que Ctrl+A no lleve Shift implícito
            return char_keysym(name.lower())
        raise ValueError(f"Tecla desconocida: {key!r}")
