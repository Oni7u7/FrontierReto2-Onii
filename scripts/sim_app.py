"""scripts/sim_app.py — App falsa dibujada + servidor RFB para simular tareas.

"ProveeNet": gestión de proveedores (otro dominio que SIGECOM, a propósito,
para detectar sobreajuste). Imita las reglas del escritorio real:
  - la pantalla repinta REPINTADO s después de cada cambio (captura vieja antes);
  - un clic con menos de 100 ms entre bajar y soltar se ignora;
  - escribir en un campo con texto concatena (Ctrl+A selecciona todo).

Pantallas: lista (buscador, "+ Nuevo proveedor", tabla con "Editar") y
formulario (Nombre, NIT, Teléfono, Ciudad, Dirección; Guardar, Cancelar y,
al editar, Eliminar).
"""
import asyncio
import copy
import os
import struct
import subprocess
import time
import unicodedata

import websockets
from Crypto.Cipher import DES
from PIL import Image, ImageDraw, ImageFont

from agent.vnc import _vnc_des_key

W, H = 1280, 800
REPINTADO = 0.9
CLICK_MIN = 0.1
HOY = "15/10/2026"

DATOS_INICIALES = [
    {"id": "PR-0001", "nombre": "Maderas del Norte", "nit": "900234567", "telefono": "3104445566", "ciudad": "Medellín", "direccion": "Cra 50 # 10-20"},
    {"id": "PR-0002", "nombre": "Luis Gómez", "nit": "79123456", "telefono": "3001112233", "ciudad": "Cali", "direccion": "Calle 5 # 38-12"},
    {"id": "PR-0003", "nombre": "Ferretería La Llave", "nit": "800555123", "telefono": "6045551234", "ciudad": "Medellín", "direccion": "Av 80 # 32-15"},
    {"id": "PR-0004", "nombre": "Textiles Andinos", "nit": "901777888", "telefono": "3157778899", "ciudad": "Bogotá", "direccion": "Calle 13 # 68-40"},
    {"id": "PR-0005", "nombre": "Luis Gómez", "nit": "1032456789", "telefono": "3209876543", "ciudad": "Barranquilla", "direccion": "Cra 46 # 72-10"},
    {"id": "PR-0006", "nombre": "Químicos del Caribe", "nit": "890123456", "telefono": "3012223344", "ciudad": "Cartagena", "direccion": "Zona Ind. Mamonal Km 3"},
    {"id": "PR-0007", "nombre": "Empaques Sur", "nit": "900888999", "telefono": "3165554433", "ciudad": "Pasto", "direccion": "Calle 18 # 25-30"},
    {"id": "PR-0008", "nombre": "Distribuidora Andes", "nit": "830444555", "telefono": "3186667788", "ciudad": "Medellín", "direccion": "Cra 43A # 1-50"},
    {"id": "PR-0009", "nombre": "Café Origen", "nit": "901222333", "telefono": "3123334455", "ciudad": "Manizales", "direccion": "Calle 23 # 21-05"},
    {"id": "PR-0010", "nombre": "Aceros Unidos", "nit": "860111000", "telefono": "3149990000", "ciudad": "Bucaramanga", "direccion": "Cra 15 # 45-60"},
]
CAMPOS = [("nombre", "Nombre"), ("nit", "NIT"), ("telefono", "Teléfono"),
          ("ciudad", "Ciudad"), ("direccion", "Dirección")]


def _fuente(size, bold=False):
    candidatos = [
        "/usr/share/fonts/TTF/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
        "/usr/share/fonts/liberation/LiberationSans-%s.ttf" % ("Bold" if bold else "Regular"),
    ]
    try:
        candidatos.append(subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"],
                                         capture_output=True, text=True, timeout=3).stdout)
    except Exception:
        pass
    for c in candidatos:
        if c and os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


F = _fuente(15)
FB = _fuente(15, bold=True)
FT = _fuente(20, bold=True)


def _norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _dentro(x, y, caja):
    x0, y0, x1, y1 = caja
    return x0 <= x <= x1 and y0 <= y <= y1


# Geometría
BUSCADOR = (130, 70, 530, 105)
BTN_BUSCAR = (545, 70, 645, 105)
BTN_NUEVO = (1040, 70, 1240, 105)
FILA_Y0, FILA_H = 196, 36
COLS = [("ID", 40), ("Nombre", 130), ("NIT", 470), ("Teléfono", 640), ("Ciudad", 820)]
BTN_EDITAR_X = (1100, 1190)
CAMPO_X = (260, 760)
CAMPO_Y0, CAMPO_DY, CAMPO_H = 140, 60, 36
BTN_GUARDAR = (260, 460, 400, 500)
BTN_CANCELAR = (420, 460, 560, 500)
BTN_ELIMINAR = (1040, 460, 1200, 500)


class AppSim:
    def __init__(self):
        self.registros = copy.deepcopy(DATOS_INICIALES)
        self.inicial = copy.deepcopy(DATOS_INICIALES)
        self.pantalla = "lista"
        self.busqueda = ""
        self.filtro = ""
        self.mensaje = ("", "")       # (texto, color)
        self.form = {}
        self.editando = None          # id o None (nuevo)
        self.foco = None              # "buscar" | nombre de campo
        self.seleccion = False        # Ctrl+A activo sobre el campo con foco
        self.ctrl = False
        self._boton_abajo = None      # (t, x, y)
        self.eventos = []             # (t, tipo, detalle)
        self.clics_ignorados = 0
        self._siguiente_id = 11
        self._frame = self._render()
        self._repintar_en = None

    # ---------------- estado visible con retraso ----------------
    def _cambio(self):
        self._repintar_en = time.monotonic() + REPINTADO

    def frame(self) -> bytes:
        if self._repintar_en is not None and time.monotonic() >= self._repintar_en:
            self._frame = self._render()
            self._repintar_en = None
        return self._frame

    # ---------------- entrada ----------------
    def puntero(self, mask, x, y):
        ahora = time.monotonic()
        if mask & 1 and self._boton_abajo is None:
            self._boton_abajo = (ahora, x, y)
        elif not mask & 1 and self._boton_abajo is not None:
            t0, _, _ = self._boton_abajo
            self._boton_abajo = None
            if ahora - t0 < CLICK_MIN:
                self.clics_ignorados += 1
                self.eventos.append((ahora, "clic_ignorado", (x, y)))
                return
            self.eventos.append((ahora, "clic", (x, y)))
            self._clic(x, y)
            self._cambio()

    def tecla(self, down, sym):
        if sym in (0xFFE3, 0xFFE4):
            self.ctrl = bool(down)
            return
        if not down or sym in (0xFFE1, 0xFFE2, 0xFFE9, 0xFFEA):
            return
        self.eventos.append((time.monotonic(), "tecla", sym))
        if self.ctrl and sym in (ord("a"), ord("A")):
            if self.foco:
                self.seleccion = True
        elif sym == 0xFF0D:
            if self.foco == "buscar":
                self._buscar()
        elif sym == 0xFF09:
            self._tab()
        elif sym == 0xFF08:
            self._editar_valor(lambda v: "" if self.seleccion else v[:-1])
        elif sym == 0xFFFF:  # Supr: borra la selección (el cursor siempre está al final)
            self._editar_valor(lambda v: "" if self.seleccion else v)
        elif sym == 0xFF1B:
            self.foco, self.seleccion = None, False
        else:
            ch = chr(sym - 0x01000000) if sym >= 0x01000000 else (chr(sym) if 32 <= sym <= 255 else "")
            if ch:
                self._editar_valor(lambda v: ch if self.seleccion else v + ch)
        self._cambio()

    def _valor(self, campo):
        return self.busqueda if campo == "buscar" else self.form.get(campo, "")

    def _editar_valor(self, fn):
        if not self.foco:
            return
        nuevo = fn(self._valor(self.foco))
        if self.foco == "buscar":
            self.busqueda = nuevo
        else:
            self.form[self.foco] = nuevo
        self.seleccion = False

    def _tab(self):
        orden = ["buscar"] if self.pantalla == "lista" else [c for c, _ in CAMPOS]
        i = orden.index(self.foco) + 1 if self.foco in orden else 0
        self.foco, self.seleccion = orden[i % len(orden)], False

    def _filas(self):
        f = _norm(self.filtro)
        return [r for r in self.registros if f in _norm(r["nombre"])]

    def _buscar(self):
        self.filtro = self.busqueda.strip()
        self.mensaje = ("", "")

    def _clic(self, x, y):
        self.seleccion = False
        if self.pantalla == "lista":
            self.foco = None
            if _dentro(x, y, BUSCADOR):
                self.foco = "buscar"
            elif _dentro(x, y, BTN_BUSCAR):
                self._buscar()
            elif _dentro(x, y, BTN_NUEVO):
                self.pantalla, self.editando, self.mensaje = "form", None, ("", "")
                self.form = {c: "" for c, _ in CAMPOS}
            else:
                for i, r in enumerate(self._filas()[:14]):
                    fy = FILA_Y0 + i * FILA_H
                    if BTN_EDITAR_X[0] <= x <= BTN_EDITAR_X[1] and fy + 4 <= y <= fy + FILA_H - 4:
                        self.pantalla, self.editando, self.mensaje = "form", r["id"], ("", "")
                        self.form = {c: r[c] for c, _ in CAMPOS}
            return
        # formulario
        self.foco = None
        for i, (c, _) in enumerate(CAMPOS):
            cy = CAMPO_Y0 + i * CAMPO_DY
            if _dentro(x, y, (CAMPO_X[0], cy, CAMPO_X[1], cy + CAMPO_H)):
                self.foco = c
                return
        if _dentro(x, y, BTN_GUARDAR):
            self._guardar()
        elif _dentro(x, y, BTN_CANCELAR):
            self.pantalla, self.mensaje = "lista", ("", "")
        elif self.editando and _dentro(x, y, BTN_ELIMINAR):
            self.registros = [r for r in self.registros if r["id"] != self.editando]
            self.pantalla, self.mensaje = "lista", (f"Proveedor {self.editando} eliminado", (180, 30, 30))

    def _guardar(self):
        f = {k: v.strip() for k, v in self.form.items()}
        if not f["nombre"]:
            self.mensaje = ("Error: el nombre es obligatorio", (200, 30, 30)); return
        if not (f["nit"].isdigit() and 6 <= len(f["nit"]) <= 12):
            self.mensaje = ("Error: NIT inválido (solo dígitos, 6 a 12)", (200, 30, 30)); return
        if f["telefono"] and not (f["telefono"].isdigit() and 7 <= len(f["telefono"]) <= 10):
            self.mensaje = ("Error: teléfono inválido (7 a 10 dígitos)", (200, 30, 30)); return
        if self.editando:
            r = next(r for r in self.registros if r["id"] == self.editando)
            r.update(f)
            texto = f"Proveedor {self.editando} actualizado"
        else:
            nid = f"PR-{self._siguiente_id:04d}"
            self._siguiente_id += 1
            self.registros.append({"id": nid, **f})
            texto = f"Proveedor {nid} creado"
        self.pantalla, self.mensaje = "lista", (texto, (20, 130, 50))
        self.filtro = self.busqueda = ""

    # ---------------- dibujo ----------------
    def _render(self) -> bytes:
        img = Image.new("RGB", (W, H), (244, 246, 249))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, 50), fill=(28, 52, 92))
        d.text((24, 14), "ProveeNet · Gestión de proveedores", font=FT, fill="white")
        d.text((1090, 17), f"Hoy: {HOY}", font=F, fill=(220, 230, 245))
        (self._render_lista if self.pantalla == "lista" else self._render_form)(d)
        return img.convert("RGBX").tobytes("raw", "BGRX")

    def _caja(self, d, caja, valor, enfocado):
        d.rectangle(caja, fill="white", outline=(40, 110, 220) if enfocado else (160, 165, 175),
                    width=2 if enfocado else 1)
        x0, y0, x1, y1 = caja
        if enfocado and self.seleccion and valor:
            tw = d.textlength(valor, font=F)
            d.rectangle((x0 + 7, y0 + 6, x0 + 9 + tw, y1 - 6), fill=(170, 200, 250))
        d.text((x0 + 8, y0 + (y1 - y0 - 18) // 2), valor + ("|" if enfocado else ""), font=F, fill="black")

    def _boton(self, d, caja, texto, color):
        d.rounded_rectangle(caja, 6, fill=color)
        tw = d.textlength(texto, font=FB)
        x0, y0, x1, y1 = caja
        d.text(((x0 + x1 - tw) / 2, y0 + (y1 - y0 - 18) / 2), texto, font=FB, fill="white")

    def _render_lista(self, d):
        d.text((40, 79), "Buscar:", font=FB, fill=(40, 40, 40))
        self._caja(d, BUSCADOR, self.busqueda, self.foco == "buscar")
        self._boton(d, BTN_BUSCAR, "Buscar", (70, 90, 120))
        self._boton(d, BTN_NUEVO, "+ Nuevo proveedor", (30, 120, 70))
        if self.mensaje[0]:
            d.text((40, 122), self.mensaje[0], font=FB, fill=self.mensaje[1])
        d.rectangle((30, 160, 1250, FILA_Y0), fill=(220, 226, 236))
        for t, x in COLS:
            d.text((x, 168), t, font=FB, fill=(30, 30, 30))
        filas = self._filas()
        for i, r in enumerate(filas[:14]):
            fy = FILA_Y0 + i * FILA_H
            d.rectangle((30, fy, 1250, fy + FILA_H), fill="white" if i % 2 == 0 else (248, 249, 251),
                        outline=(225, 228, 233))
            for (_, x), k in zip(COLS, ["id", "nombre", "nit", "telefono", "ciudad"]):
                d.text((x, fy + 9), r[k], font=F, fill=(20, 20, 20))
            self._boton(d, (BTN_EDITAR_X[0], fy + 5, BTN_EDITAR_X[1], fy + FILA_H - 5), "Editar", (60, 100, 170))
        pie = FILA_Y0 + min(len(filas), 14) * FILA_H + 12
        d.text((40, pie), f"{len(filas)} proveedor(es)" + (f" — filtro: «{self.filtro}»" if self.filtro else ""),
               font=F, fill=(90, 90, 90))

    def _render_form(self, d):
        titulo = f"Editar proveedor {self.editando}" if self.editando else "Nuevo proveedor"
        d.text((60, 80), titulo, font=FT, fill=(28, 52, 92))
        for i, (c, etiqueta) in enumerate(CAMPOS):
            cy = CAMPO_Y0 + i * CAMPO_DY
            d.text((60, cy + 9), etiqueta + ":", font=FB, fill=(40, 40, 40))
            self._caja(d, (CAMPO_X[0], cy, CAMPO_X[1], cy + CAMPO_H), self.form.get(c, ""), self.foco == c)
        self._boton(d, BTN_GUARDAR, "Guardar", (30, 120, 70))
        self._boton(d, BTN_CANCELAR, "Cancelar", (120, 120, 120))
        if self.editando:
            self._boton(d, BTN_ELIMINAR, "Eliminar", (190, 40, 40))
        if self.mensaje[0]:
            d.text((260, 520), self.mensaje[0], font=FB, fill=self.mensaje[1])

    # ---------------- evaluación ----------------
    def diff(self):
        """Cambios respecto a los datos iniciales, en el formato de pruebas/."""
        ini = {r["id"]: r for r in self.inicial}
        fin = {r["id"]: r for r in self.registros}
        cambios = []
        for rid, r in fin.items():
            if rid not in ini:
                cambios += [{"id": "nuevo", "campo": k, "accion": "create", "valor": v}
                            for k, v in r.items() if k != "id" and v]
            else:
                cambios += [{"id": rid, "campo": k, "accion": "update", "valor": v}
                            for k, v in r.items() if v != ini[rid][k]]
        cambios += [{"id": rid, "campo": "*", "accion": "delete", "valor": None}
                    for rid in ini if rid not in fin]
        return cambios

    def duplicados(self):
        nits = [r["nit"] for r in self.registros]
        return sorted({n for n in nits if nits.count(n) > 1})


async def servir(app_ref, password, host="127.0.0.1", port=0):
    """Servidor RFB 3.8 mínimo (auth tipo 2, Raw, 32 bpp BGRX).
    `app_ref` es una lista de un elemento para poder cambiar de app entre tareas."""

    async def handler(ws):
        buf = bytearray()

        async def rd(n):
            while len(buf) < n:
                buf.extend(await ws.recv())
            out = bytes(buf[:n]); del buf[:n]
            return out

        try:
            await ws.send(b"RFB 003.008\n"); await rd(12)
            await ws.send(bytes([1, 2])); await rd(1)
            ch = os.urandom(16); await ws.send(ch)
            ok = await rd(16) == DES.new(_vnc_des_key(password), DES.MODE_ECB).encrypt(ch)
            await ws.send(struct.pack("!I", 0 if ok else 1))
            if not ok:
                await ws.send(struct.pack("!I", 4) + b"fail"); return
            await rd(1)
            await ws.send(struct.pack("!HH", W, H) + bytes(16) + struct.pack("!I", 3) + b"sim")
            while True:
                t = (await rd(1))[0]
                if t == 0:
                    await rd(19)
                elif t == 2:
                    n = struct.unpack("!H", (await rd(3))[1:])[0]; await rd(4 * n)
                elif t == 3:
                    await rd(9)
                    await ws.send(struct.pack("!BxH", 0, 1) + struct.pack("!HHHHi", 0, 0, W, H, 0)
                                  + app_ref[0].frame())
                elif t == 4:
                    dflag, sym = struct.unpack("!BxxI", await rd(7))
                    app_ref[0].tecla(dflag, sym)
                elif t == 5:
                    mask, x, y = struct.unpack("!BHH", await rd(5))
                    app_ref[0].puntero(mask, x, y)
                elif t == 6:
                    await rd(3); n = struct.unpack("!I", await rd(4))[0]; await rd(n)
        except websockets.ConnectionClosed:
            pass

    return await websockets.serve(handler, host, port, subprotocols=["binary"], max_size=None)
