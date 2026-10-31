"""tests/test_vnc.py — Adaptación de scripts/test_vnc_mock.py a pytest."""
import asyncio
import io
import os
import struct
import sys
from pathlib import Path

import pytest
import websockets
from Crypto.Cipher import DES
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.vnc import Desktop, _vnc_des_key

W, H, PASS = 64, 32, "secreto1"
events = []


async def _mock_server(ws):
    buf = bytearray()

    async def rd(n):
        while len(buf) < n:
            buf.extend(await ws.recv())
        out = bytes(buf[:n])
        del buf[:n]
        return out

    await ws.send(b"RFB 003.008\n")
    assert (await rd(12)) == b"RFB 003.008\n"

    await ws.send(bytes([1, 2]))
    assert (await rd(1)) == b"\x02"

    ch = os.urandom(16)
    await ws.send(ch[:5])
    await ws.send(ch[5:])
    resp = await rd(16)
    ok = resp == DES.new(_vnc_des_key(PASS), DES.MODE_ECB).encrypt(ch)
    await ws.send(struct.pack("!I", 0 if ok else 1))
    if not ok:
        await ws.send(struct.pack("!I", 4) + b"fail")
        return

    assert (await rd(1)) == b"\x01"
    name = b"mock"
    await ws.send(
        struct.pack("!HH", W, H)
        + bytes(16)
        + struct.pack("!I", len(name))
        + name
    )

    px = bytearray()
    for y in range(H):
        px += (
            bytes([0, 0, 255, 0]) if y < H // 2 else bytes([255, 0, 0, 0])
        ) * W

    while True:
        t = (await rd(1))[0]
        if t == 0:
            await rd(19)
        elif t == 2:
            n = struct.unpack("!H", (await rd(3))[1:])[0]
            await rd(4 * n)
        elif t == 3:
            await rd(9)
            await ws.send(b"\x02")  # Bell intercalado
            await ws.send(
                struct.pack("!BxH", 0, 1)
                + struct.pack("!HHHHi", 0, 0, W, H, 0)
                + bytes(px[:1000])
            )
            await ws.send(bytes(px[1000:]))
        elif t == 5:
            events.append(("ptr",) + struct.unpack("!BHH", await rd(5)))
        elif t == 4:
            d, k = struct.unpack("!BxxI", await rd(7))
            events.append(("key", d, k))


@pytest.mark.asyncio
async def test_desktop_full_cycle():
    global events
    events = []

    server = await websockets.serve(
        _mock_server, "127.0.0.1", 8765, subprotocols=["binary"]
    )
    try:
        d = Desktop("ws://127.0.0.1:8765", PASS)
        await d.connect()
        assert (d.width, d.height) == (W, H)

        png = await d.screenshot()
        img = Image.open(io.BytesIO(png))
        assert img.size == (W, H)
        assert img.getpixel((3, 2)) == (255, 0, 0), img.getpixel((3, 2))
        assert img.getpixel((3, H - 2)) == (0, 0, 255)

        await d.click(10, 20)
        await d.type_text("Añ1", select_all=True)
        await asyncio.sleep(0.3)
        await d.close()

        ptr = [e for e in events if e[0] == "ptr"]
        assert [p[1] for p in ptr][-3:] == [0, 1, 0]
        assert ptr[-1][2:] == (10, 20)

        keys = [e[2] for e in events if e[0] == "key" and e[1] == 1]
        assert keys == [0xFFE3, 0x61, ord("A"), 0xF1, ord("1")], [
            hex(k) for k in keys
        ]

        # Auth fallida
        bad = Desktop("ws://127.0.0.1:8765", "mala")
        with pytest.raises(Exception) as exc_info:
            await bad.connect()
        assert "Autenticacion" in str(exc_info.value)
    finally:
        server.close()
        await server.wait_closed()
