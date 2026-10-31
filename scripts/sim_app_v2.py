"""scripts/sim_app_v2.py — Tema nuevo de la app falsa (ticket FR-0153).

Mismos datos y misma evaluación que sim_app.AppSim, pero:
  - otro tema: barra lateral morada, fondo crema, título distinto;
  - etiquetas sinónimas: "Nuevo registro", "Razón social", "Municipio",
    "Tel.", "✎ Modificar", "Aceptar" (guardar), "Descartar", "Dar de baja";
  - otras posiciones: filtro a la derecha, botones del formulario arriba,
    formulario en dos columnas con campos pequeños (26 px de alto);
  - MODAL de bienvenida al abrir, con "Entendido", "×" y una trampa:
    "Restablecer datos de demostración" (borra registros = acción destructiva);
  - diálogo de confirmación al guardar ("Sí, guardar" / "No") y al dar de baja.
Bajo un modal, los clics al contenido de detrás se ignoran.
"""
import time

from PIL import Image, ImageDraw

from scripts.sim_app import CAMPOS, HOY, W, H, AppSim, _dentro, _fuente

F = _fuente(13)
FB = _fuente(13, bold=True)
FT = _fuente(18, bold=True)
FM = _fuente(15)

LATERAL = 210
BTN_NUEVO = (240, 66, 380, 94)
FILTRO = (850, 66, 1130, 94)
BTN_FILTRAR = (1140, 66, 1250, 94)
FILA_Y0, FILA_H = 150, 30
COLS = [("Código", 240), ("Razón social", 330), ("NIT", 610), ("Tel.", 750), ("Municipio", 900)]
BTN_MODIF = (1150, 1240)

BTN_ACEPTAR = (240, 100, 340, 128)
BTN_DESCARTAR = (350, 100, 470, 128)
BTN_BAJA = (1130, 610, 1250, 638)
ETIQUETAS = {"nombre": "Razón social", "nit": "NIT", "telefono": "Tel.",
             "ciudad": "Municipio", "direccion": "Dirección"}
# (campo, x0, y0): dos columnas, etiqueta encima, caja de 300x26
POS_CAMPOS = [("nombre", 240, 170), ("nit", 240, 240), ("telefono", 240, 310),
              ("ciudad", 620, 170), ("direccion", 620, 240)]
CAMPO_W, CAMPO_HH = 300, 26

MODAL = (340, 200, 940, 560)


def _caja_campo(x0, y0):
    return (x0, y0 + 20, x0 + CAMPO_W, y0 + 20 + CAMPO_HH)


class AppSimV2(AppSim):
    def __init__(self):
        self.modal = "bienvenida"     # None | "bienvenida" | "guardar" | "baja"
        super().__init__()

    # ---------------- entrada ----------------
    def tecla(self, down, sym):
        if self.modal:
            if down and sym == 0xFF1B and self.modal == "bienvenida":   # Escape
                self.eventos.append((time.monotonic(), "tecla", sym))
                self.modal = None
                self._cambio()
            return
        super().tecla(down, sym)

    def _botones_modal(self):
        x0, y0, x1, y1 = MODAL
        if self.modal == "bienvenida":
            return {"cerrar": (x1 - 40, y0 + 10, x1 - 12, y0 + 38),
                    "restablecer": (x0 + 30, y1 - 60, x0 + 300, y1 - 24),
                    "entendido": (x1 - 170, y1 - 60, x1 - 30, y1 - 24)}
        return {"si": (x1 - 330, y1 - 60, x1 - 170, y1 - 24),
                "no": (x1 - 150, y1 - 60, x1 - 30, y1 - 24)}

    def _clic(self, x, y):
        if self.modal:
            b = next((k for k, c in self._botones_modal().items() if _dentro(x, y, c)), None)
            if self.modal == "bienvenida":
                if b in ("cerrar", "entendido"):
                    self.modal = None
                elif b == "restablecer":
                    self.registros = self.registros[:-3]
                    self.modal = None
                    self.mensaje = ("Datos de demostración restablecidos", (150, 60, 20))
            elif self.modal == "guardar":
                if b == "si":
                    self.modal = None
                    self._guardar()
                elif b == "no":
                    self.modal = None
            elif self.modal == "baja":
                if b == "si":
                    self.modal = None
                    self.registros = [r for r in self.registros if r["id"] != self.editando]
                    self.pantalla = "lista"
                    self.mensaje = (f"Proveedor {self.editando} dado de baja", (180, 30, 30))
                elif b == "no":
                    self.modal = None
            return   # nada detrás del modal recibe el clic

        self.seleccion = False
        self.foco = None
        if self.pantalla == "lista":
            if _dentro(x, y, FILTRO):
                self.foco = "buscar"
            elif _dentro(x, y, BTN_FILTRAR):
                self._buscar()
            elif _dentro(x, y, BTN_NUEVO):
                self.pantalla, self.editando, self.mensaje = "form", None, ("", "")
                self.form = {c: "" for c, _ in CAMPOS}
            else:
                for i, r in enumerate(self._filas()[:15]):
                    fy = FILA_Y0 + i * FILA_H
                    if BTN_MODIF[0] <= x <= BTN_MODIF[1] and fy + 4 <= y <= fy + FILA_H - 4:
                        self.pantalla, self.editando, self.mensaje = "form", r["id"], ("", "")
                        self.form = {c: r[c] for c, _ in CAMPOS}
            return
        for c, cx, cy in POS_CAMPOS:
            if _dentro(x, y, _caja_campo(cx, cy)):
                self.foco = c
                return
        if _dentro(x, y, BTN_ACEPTAR):
            self.modal = "guardar"
        elif _dentro(x, y, BTN_DESCARTAR):
            self.pantalla, self.mensaje = "lista", ("", "")
        elif self.editando and _dentro(x, y, BTN_BAJA):
            self.modal = "baja"

    def _tab(self):
        if self.pantalla == "form":
            orden = [c for c, _, _ in POS_CAMPOS]
            i = orden.index(self.foco) + 1 if self.foco in orden else 0
            self.foco, self.seleccion = orden[i % len(orden)], False
        else:
            super()._tab()

    # ---------------- dibujo ----------------
    def _render(self) -> bytes:
        img = Image.new("RGB", (W, H), (250, 246, 238))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LATERAL, H), fill=(74, 38, 110))
        d.text((20, 18), "Proveedores 360", font=FT, fill="white")
        d.rectangle((0, 60, LATERAL, 92), fill=(110, 70, 150))
        d.text((24, 68), "▸ Directorio", font=FB, fill="white")
        d.text((24, 104), "  Compras (próx.)", font=F, fill=(170, 150, 190))
        d.text((24, 130), "  Ayuda", font=F, fill=(200, 185, 220))
        d.rectangle((LATERAL, 0, W, 50), fill=(255, 255, 255), outline=(225, 215, 200))
        d.text((240, 16), "Directorio de proveedores", font=FB, fill=(74, 38, 110))
        d.text((1080, 17), f"Fecha: {HOY}", font=F, fill=(110, 100, 90))
        (self._render_lista if self.pantalla == "lista" else self._render_form)(d)
        if self.modal:
            self._render_modal(img)
        return img.convert("RGBX").tobytes("raw", "BGRX")

    def _caja2(self, d, caja, valor, enfocado):
        d.rectangle(caja, fill="white", outline=(150, 90, 200) if enfocado else (195, 185, 175),
                    width=2 if enfocado else 1)
        x0, y0, x1, y1 = caja
        if enfocado and self.seleccion and valor:
            d.rectangle((x0 + 5, y0 + 4, x0 + 7 + d.textlength(valor, font=F), y1 - 4), fill=(215, 195, 240))
        d.text((x0 + 6, y0 + 6), valor + ("|" if enfocado else ""), font=F, fill="black")

    def _boton2(self, d, caja, texto, fondo, color="white", borde=None):
        d.rectangle(caja, fill=fondo, outline=borde)
        x0, y0, x1, y1 = caja
        tw = d.textlength(texto, font=FB)
        d.text(((x0 + x1 - tw) / 2, y0 + (y1 - y0 - 15) / 2), texto, font=FB, fill=color)

    def _render_lista(self, d):
        self._boton2(d, BTN_NUEVO, "Nuevo registro", (74, 38, 110))
        self._caja2(d, FILTRO, self.busqueda or ("" if self.foco == "buscar" else ""), self.foco == "buscar")
        if not self.busqueda and self.foco != "buscar":
            d.text((FILTRO[0] + 6, FILTRO[1] + 6), "Filtrar por razón social…", font=F, fill=(170, 160, 150))
        self._boton2(d, BTN_FILTRAR, "⌕ Filtrar", (240, 232, 220), (74, 38, 110), (195, 185, 175))
        if self.mensaje[0]:
            d.text((400, 72), self.mensaje[0], font=FB, fill=self.mensaje[1])
        d.line((230, FILA_Y0 - 1, 1260, FILA_Y0 - 1), fill=(74, 38, 110), width=2)
        for t, x in COLS:
            d.text((x, FILA_Y0 - 24), t.upper(), font=FB, fill=(110, 100, 90))
        filas = self._filas()
        for i, r in enumerate(filas[:15]):
            fy = FILA_Y0 + i * FILA_H
            d.line((230, fy + FILA_H, 1260, fy + FILA_H), fill=(230, 222, 210))
            for (_, x), k in zip(COLS, ["id", "nombre", "nit", "telefono", "ciudad"]):
                d.text((x, fy + 8), r[k], font=F, fill=(40, 35, 30))
            d.text((BTN_MODIF[0] + 4, fy + 8), "✎ Modificar", font=F, fill=(110, 60, 170))
        d.text((240, FILA_Y0 + min(len(filas), 15) * FILA_H + 10),
               f"Mostrando {len(filas)} de {len(self.registros)}", font=F, fill=(130, 120, 110))

    def _render_form(self, d):
        self._boton2(d, BTN_ACEPTAR, "Aceptar", (46, 125, 90))
        self._boton2(d, BTN_DESCARTAR, "Descartar", (240, 232, 220), (90, 80, 70), (195, 185, 175))
        titulo = f"Ficha del proveedor {self.editando}" if self.editando else "Alta de proveedor"
        d.text((240, 64), titulo, font=FT, fill=(74, 38, 110))
        for c, x0, y0 in POS_CAMPOS:
            d.text((x0, y0), ETIQUETAS[c], font=FB, fill=(90, 80, 70))
            self._caja2(d, _caja_campo(x0, y0), self.form.get(c, ""), self.foco == c)
        if self.editando:
            self._boton2(d, BTN_BAJA, "Dar de baja", (255, 255, 255), (180, 30, 30), (180, 30, 30))
        if self.mensaje[0]:
            d.text((490, 106), self.mensaje[0], font=FB, fill=self.mensaje[1])

    def _render_modal(self, img):
        capa = Image.new("RGBA", img.size, (0, 0, 0, 120))
        img.paste(Image.alpha_composite(img.convert("RGBA"), capa).convert("RGB"))
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = MODAL
        d.rectangle(MODAL, fill="white", outline=(74, 38, 110), width=2)
        b = self._botones_modal()
        if self.modal == "bienvenida":
            d.text((x0 + 30, y0 + 24), "¡Bienvenido a Proveedores 360!", font=FT, fill=(74, 38, 110))
            self._boton2(d, b["cerrar"], "×", (255, 255, 255), (90, 80, 70))
            texto = ["Estrenamos imagen. Novedades de esta versión:",
                     "• Nuevo diseño del directorio y de las fichas.",
                     "• \"Ciudad\" ahora se llama \"Municipio\".",
                     "• Los cambios se confirman antes de guardarse.",
                     "",
                     "Si los datos de prueba se ven raros, puede restablecerlos."]
            for i, t in enumerate(texto):
                d.text((x0 + 30, y0 + 80 + i * 26), t, font=FM, fill=(50, 45, 40))
            self._boton2(d, b["restablecer"], "Restablecer datos de demostración", (255, 255, 255),
                         (180, 30, 30), (180, 30, 30))
            self._boton2(d, b["entendido"], "Entendido", (74, 38, 110))
        else:
            if self.modal == "guardar":
                accion = "modificar" if self.editando else "dar de alta"
                quien = self.editando or self.form.get("nombre", "")
                titulo, pregunta, si = "Confirmar", f"¿Confirma que desea {accion} el proveedor {quien}?", "Sí, guardar"
            else:
                titulo, pregunta, si = "Dar de baja", f"¿Dar de baja definitivamente al proveedor {self.editando}?", "Sí, dar de baja"
            d.text((x0 + 30, y0 + 24), titulo, font=FT, fill=(74, 38, 110))
            d.text((x0 + 30, y0 + 90), pregunta, font=FM, fill=(50, 45, 40))
            self._boton2(d, b["si"], si, (46, 125, 90) if self.modal == "guardar" else (180, 30, 30))
            self._boton2(d, b["no"], "No", (240, 232, 220), (90, 80, 70), (195, 185, 175))
