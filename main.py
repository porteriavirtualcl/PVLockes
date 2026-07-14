"""
main.py
-------
Aplicación principal "Portería Virtual" (GUI Tkinter para pantalla táctil).

Arquitectura OFFLINE-FIRST:
    ConfigManager     -> configuración, tipo de sistema y condominio
    HardwareManager   -> accionar cerraduras (GPIO / mock)
    LocalStore        -> SQLite: fuente de verdad local (residentes + encomiendas)
    FirebaseService   -> espejo remoto (esquema de producción)
    SyncService       -> sincroniza local <-> Firebase en segundo plano
    ResourceAllocator -> asigna locker/buzón según tamaño y ocupación real

El kiosco OPERA SIN INTERNET: valida unidades y registra encomiendas contra la
base local, y sincroniza con Firebase cuando hay conexión.

Flujo "Dejar Encomienda":
    1. Ingresar N° de Depto/Casa.
    2. Elegir el residente destinatario (solo residentes de ESTE condominio).
    3. Si es mixto, elegir tamaño (Chica->buzón, Mediana/Grande->locker).
    4. Abrir GPIO de depósito -> registrar local -> sincronizar.
       El residente ve el QR de retiro en su app (valor = parcel_id).
"""

import logging
import tkinter as tk
from tkinter import font as tkfont

from config_manager import ConfigManager, ConfigError
from hardware_manager import HardwareManager
from local_store import LocalStore
from firebase_service import FirebaseService, FirebaseNoDisponibleError
from sync_service import SyncService
from scanner_listener import ScannerListener
from command_listener import CommandListener
from resource_allocator import ResourceAllocator, SinDisponibilidadError

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Paleta de colores — tema claro moderno, alineado al logo de marca.
COLOR_FONDO = "#F4F7FA"       # fondo claro
COLOR_TARJETA = "#FFFFFF"     # tarjetas / paneles
COLOR_MARCA = "#29ABE2"       # azul de marca (del logo)
COLOR_MARCA_OSC = "#1B8FC4"   # azul de marca, tono presionado
COLOR_ACENTO = COLOR_MARCA    # alias para el resto de pantallas
COLOR_TEXTO = "#33414F"       # texto oscuro sobre fondo claro
COLOR_BOTON_TEXTO = "#FFFFFF"  # texto sobre botones de color
COLOR_OK = "#27AE60"
COLOR_ERROR = "#E74C3C"
COLOR_MORADO = "#7C4DBC"      # botón "Retirar"
COLOR_TENUE = "#8A9BA8"       # texto secundario / muted
COLOR_GRIS = "#B0BEC9"        # botones neutros (cancelar)


class PorteriaApp(tk.Tk):
    """Ventana raíz que gestiona la navegación entre pantallas (frames)."""

    def __init__(self):
        super().__init__()

        # --- Servicios ---
        self.config_mgr = ConfigManager("config.json")
        self.local_store = LocalStore(self.config_mgr.db_local)
        # Aplicar la config remota cacheada (kiosks/{kiosk_id}) ANTES de armar el
        # hardware y los recursos, para que reflejen lo administrado en el backend.
        self.config_mgr.aplicar_config_remota(self.local_store.get_kiosk_config())

        self.hardware = HardwareManager(self.config_mgr.as_dict())
        self.allocator = ResourceAllocator(self.config_mgr, self.local_store)

        # Firebase se conecta de forma diferida y tolerante a fallos.
        self.firebase = None
        self._conectar_firebase()

        # SyncService: hilo de fondo que empuja/pull entre local y Firebase.
        self.sync = SyncService(
            local_store=self.local_store,
            firebase=self.firebase,
            condo_id=self.config_mgr.condo_id,
            intervalo_seg=self.config_mgr.intervalo_sync_seg,
            max_reintentos=self.config_mgr.max_reintentos,
            kiosk_id=self.config_mgr.kiosk_id,
        )
        self.sync.iniciar()

        # Lector de retiro automático (opcional; lockers de doble puerta).
        self.scanner = None
        if self.config_mgr.retiro_auto_habilitado:
            self.scanner = ScannerListener(
                dispositivo=self.config_mgr.retiro_auto_dispositivo,
                on_scan=self._on_scan_retiro,
            )
            self.scanner.iniciar()

        # Apertura remota: listener de comandos del operador (tiempo real).
        self.command_listener = CommandListener(
            firebase=self.firebase,
            kiosk_id=self.config_mgr.kiosk_id,
            on_abrir=self._abrir_remoto,
        )
        self.command_listener.iniciar()

        # --- Estado del flujo en curso ---
        self.datos_flujo = {}
        # Id del temporizador de auto-retorno al inicio (pantallas de resultado).
        self._auto_return_id = None

        # --- Ventana (orientación configurable: vertical u horizontal) ---
        self.vertical = self.config_mgr.es_vertical
        self.title(self.config_mgr.nombre_sistema)
        self.configure(bg=COLOR_FONDO)
        self.attributes("-fullscreen", self.config_mgr.pantalla_completa)
        self.geometry("480x800" if self.vertical else "800x480")
        self.bind("<Escape>", lambda e: self._salir())
        self.protocol("WM_DELETE_WINDOW", self._salir)

        # Fuentes — más compactas en vertical (480px de ancho) para evitar cortes.
        if self.vertical:
            self.f_titulo = tkfont.Font(family="Helvetica", size=22, weight="bold")
            self.f_boton = tkfont.Font(family="Helvetica", size=15, weight="bold")
            self.f_texto = tkfont.Font(family="Helvetica", size=13)
            self.f_pie = tkfont.Font(family="Helvetica", size=10)
        else:
            self.f_titulo = tkfont.Font(family="Helvetica", size=34, weight="bold")
            self.f_boton = tkfont.Font(family="Helvetica", size=20, weight="bold")
            self.f_texto = tkfont.Font(family="Helvetica", size=16)
            self.f_pie = tkfont.Font(family="Helvetica", size=11)

        # Contenedor de frames.
        self.contenedor = tk.Frame(self, bg=COLOR_FONDO)
        self.contenedor.pack(fill="both", expand=True)

        self.mostrar_principal()

    # ================================================================== #
    # Infraestructura de navegación
    # ================================================================== #
    def _limpiar(self):
        # Cancela cualquier auto-retorno pendiente antes de cambiar de pantalla.
        if self._auto_return_id is not None:
            self.after_cancel(self._auto_return_id)
            self._auto_return_id = None
        for widget in self.contenedor.winfo_children():
            widget.destroy()

    def _conectar_firebase(self):
        try:
            self.firebase = FirebaseService()
        except FirebaseNoDisponibleError as e:
            logger.warning("Firebase no disponible al iniciar (se opera offline): %s", e)
            self.firebase = None

    def _boton(self, parent, texto, comando, color=COLOR_ACENTO, **kw):
        return tk.Button(
            parent, text=texto, command=comando, font=self.f_boton,
            bg=color, fg=COLOR_BOTON_TEXTO, activebackground=color,
            activeforeground=COLOR_BOTON_TEXTO, relief="flat", bd=0,
            cursor="hand2", padx=20, pady=15, **kw,
        )

    # ------------------------------------------------------------------ #
    # Helpers visuales: logo + botones tipo tarjeta redondeada
    # ------------------------------------------------------------------ #
    def _cargar_logo(self, alto_px: int):
        """Carga y escala el logo (Pillow). Devuelve un PhotoImage o None."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            logger.warning("Pillow no disponible; se omite el logo.")
            return None
        import os
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.jpg")
        if not os.path.exists(ruta):
            return None
        img = Image.open(ruta).convert("RGBA")
        ratio = alto_px / img.height
        img = img.resize((int(img.width * ratio), alto_px), Image.LANCZOS)

        # Hacer transparente el fondo blanco del logo para que no se note el cuadrado.
        px = img.load()
        ancho, alto = img.size
        for y in range(alto):
            for x in range(ancho):
                r, g, b, a = px[x, y]
                if r > 238 and g > 238 and b > 238:
                    px[x, y] = (r, g, b, 0)      # blanco -> transparente

        return ImageTk.PhotoImage(img)

    @staticmethod
    def _rect_redondeado(canvas, x1, y1, x2, y2, r, **kw):
        """Dibuja un rectángulo de esquinas redondeadas en un Canvas."""
        puntos = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return canvas.create_polygon(puntos, smooth=True, **kw)

    def _boton_tarjeta(self, parent, emoji, titulo, subtitulo, comando, color,
                       w=300, h=190):
        """Botón grande tipo tarjeta redondeada (ícono + título + subtítulo)."""
        r = 26
        cv = tk.Canvas(parent, width=w, height=h, bg=COLOR_FONDO,
                       highlightthickness=0, cursor="hand2")
        self._rect_redondeado(cv, 2, 2, w - 2, h - 2, r, fill=color, outline=color)
        cv.create_text(w / 2, 55, text=emoji, font=("Helvetica", 44), fill=COLOR_BOTON_TEXTO)
        cv.create_text(w / 2, 120, text=titulo, font=("Helvetica", 20, "bold"),
                       fill=COLOR_BOTON_TEXTO)
        cv.create_text(w / 2, 152, text=subtitulo, font=("Helvetica", 12),
                       fill=COLOR_BOTON_TEXTO)
        cv.bind("<Button-1>", lambda e: comando())
        return cv

    # ================================================================== #
    # Pantalla principal
    # ================================================================== #
    def mostrar_principal(self):
        self._limpiar()
        self.datos_flujo = {}

        # Logo de marca (o texto de respaldo si no está Pillow/el archivo).
        self._logo_img = self._cargar_logo(alto_px=150)
        if self._logo_img is not None:
            tk.Label(self.contenedor, image=self._logo_img,
                     bg=COLOR_FONDO).pack(pady=(30, 6))
        else:
            tk.Label(self.contenedor, text=self.config_mgr.nombre_sistema,
                     font=self.f_titulo, bg=COLOR_FONDO, fg=COLOR_MARCA).pack(pady=(40, 6))

        tk.Label(self.contenedor, text=self.config_mgr.condo_name,
                 font=("Helvetica", 22, "bold"), bg=COLOR_FONDO,
                 fg=COLOR_TEXTO).pack(pady=(0, 24))

        # Tarjetas de acción: apiladas en vertical, lado a lado en horizontal.
        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(pady=10)

        ancho = 380 if self.vertical else 300
        b1 = self._boton_tarjeta(marco, "📦", "Dejar", "Encomienda",
                                 self.iniciar_dejar, COLOR_MARCA, w=ancho)
        b2 = self._boton_tarjeta(marco, "📤", "Retirar", "Encomienda",
                                 self.iniciar_retirar, COLOR_MORADO, w=ancho)
        if self.vertical:
            b1.grid(row=0, column=0, pady=12)
            b2.grid(row=1, column=0, pady=12)
        else:
            b1.grid(row=0, column=0, padx=18)
            b2.grid(row=0, column=1, padx=18)

        self._pie_estado()

    def _pie_estado(self):
        """Muestra un indicador discreto de conexión y pendientes de sync."""
        pendientes = self.local_store.contar_pendientes_sync()
        online = self.firebase is not None and self.firebase.conectado
        estado = "🟢 En línea" if online else "🟠 Sin conexión (operando offline)"
        if pendientes:
            estado += f"  ·  {pendientes} por sincronizar"
        tk.Label(self.contenedor, text=estado, font=self.f_pie,
                 bg=COLOR_FONDO, fg=COLOR_TENUE).pack(side="bottom", pady=8)

    # ================================================================== #
    # Flujo: Dejar Encomienda
    # ================================================================== #
    def iniciar_dejar(self):
        """Paso 1: solicitar N° de Depto/Casa (según configuración)."""
        self.datos_flujo = {"accion": "dejar"}
        self._pantalla_teclado(
            titulo=f"Ingrese {self.config_mgr.etiqueta_unidad}",
            on_confirmar=self._buscar_unidad,
        )

    def _buscar_unidad(self, unidad_id: str):
        """Paso 1b: buscar residentes de la unidad en la base LOCAL (offline)."""
        if not unidad_id:
            return

        etiqueta = self.config_mgr.etiqueta_unidad

        # Si nunca se sincronizó (caché vacía), avisar en vez de "no encontrado".
        if self.local_store.contar_residentes() == 0:
            self._pantalla_resultado(
                titulo="Preparando sistema",
                mensaje=("Aún no se han sincronizado los residentes.\n"
                         "Verifica la conexión e intenta en unos minutos."),
                color=COLOR_ACENTO,
            )
            return

        residentes = self.local_store.get_residentes_por_unidad(unidad_id)
        if not residentes:
            self._pantalla_resultado(
                titulo=f"{etiqueta} sin residentes",
                mensaje=(f"El/la {etiqueta} {unidad_id} no tiene residentes "
                         f"activos registrados en este condominio."),
                color=COLOR_ERROR,
            )
            return

        self.datos_flujo["unidad_id"] = unidad_id
        self._pantalla_nombres(unidad_id, residentes)

    def _pantalla_nombres(self, unidad_id: str, residentes: list):
        """Paso 1c: mostrar los nombres disponibles; el repartidor elige uno."""
        self._limpiar()
        etiqueta = self.config_mgr.etiqueta_unidad

        tk.Label(self.contenedor, text=f"{etiqueta} {unidad_id}",
                 font=self.f_titulo, bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(25, 5))
        tk.Label(self.contenedor, text="Seleccione el destinatario",
                 font=self.f_texto, bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(0, 20))

        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(fill="both", expand=True, padx=40)

        for residente in residentes:
            nombre = residente.get("nombre", "Sin nombre")
            self._boton(
                marco, nombre,
                lambda r=residente: self._seleccionar_residente(r),
                color=COLOR_ACENTO,
            ).pack(fill="x", pady=5)

        self._boton(self.contenedor, "← Cancelar", self.mostrar_principal,
                    color=COLOR_GRIS).pack(side="bottom", pady=15)

    def _seleccionar_residente(self, residente: dict):
        """El repartidor eligió un nombre: elegir la empresa de reparto."""
        self.datos_flujo["residente"] = residente
        self._pantalla_courier()

    def _pantalla_courier(self):
        """Paso: elegir la empresa de reparto (courier)."""
        self._limpiar()
        tk.Label(self.contenedor, text="Empresa de reparto",
                 font=self.f_titulo, bg=COLOR_FONDO, fg=COLOR_TEXTO,
                 wraplength=440).pack(pady=(30, 20))

        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(fill="both", expand=True, padx=30)

        # Couriers administrados en el backend (Firestore, cacheados localmente).
        # Fallback a la lista de config.json si aún no se ha sincronizado.
        couriers = self.local_store.get_couriers() or self.config_mgr.couriers

        cols = 2
        for c in range(cols):
            marco.columnconfigure(c, weight=1)
        for i, nombre in enumerate(couriers):
            fila, col = divmod(i, cols)
            self._boton(marco, nombre,
                        lambda x=nombre: self._seleccionar_courier(x),
                        color=COLOR_MARCA).grid(row=fila, column=col, padx=8, pady=8, sticky="ew")

        self._boton(self.contenedor, "← Cancelar", self.mostrar_principal,
                    color=COLOR_GRIS).pack(side="bottom", pady=15)

    def _seleccionar_courier(self, courier: str):
        """Courier elegido: continuar con tamaño (si mixto) o asignación."""
        self.datos_flujo["courier"] = courier
        if self.config_mgr.es_mixto():
            self._pantalla_tamano()
        else:
            tamano = "chica" if self.config_mgr.tipo_sistema == "buzon" else "mediana"
            self._procesar_asignacion(tamano)

    def _pantalla_tamano(self):
        """Paso 2 (solo mixto): elegir tamaño de encomienda."""
        self._limpiar()
        tk.Label(self.contenedor, text="Tamaño de la encomienda",
                 font=self.f_titulo, bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(40, 30))

        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack()

        # Lockers son de tamaño uniforme: en mixto solo se distingue
        # Chico (buzón) vs Mediano/normal (locker).
        opciones = [
            ("📮 Chico\n(Buzón)", "chica", "#16a085"),
            ("📦 Mediano\n(Locker)", "mediana", COLOR_ACENTO),
        ]
        for i, (texto, tamano, color) in enumerate(opciones):
            btn = self._boton(marco, texto, lambda t=tamano: self._procesar_asignacion(t),
                              color=color, width=12, height=2)
            if self.vertical:
                btn.grid(row=i, column=0, pady=8)
            else:
                btn.grid(row=0, column=i, padx=15)

        self._boton(self.contenedor, "← Cancelar", self.mostrar_principal,
                    color=COLOR_GRIS).pack(side="bottom", pady=20)

    def _procesar_asignacion(self, tamano: str):
        """Paso 3: asignar recurso, abrir GPIO, registrar local y sincronizar."""
        try:
            recurso = self.allocator.asignar(tamano)
        except (SinDisponibilidadError, ValueError) as e:
            self._pantalla_resultado("Sin disponibilidad", str(e), COLOR_ERROR)
            return

        # 3a. Accionar cerradura de depósito.
        try:
            self.hardware.abrir_cerradura(recurso["id"], "deposito")
        except ValueError as e:
            self._pantalla_resultado("Error de hardware", str(e), COLOR_ERROR)
            return

        # 3b. Registrar la encomienda en la base LOCAL (fuente de verdad).
        residente = self.datos_flujo["residente"]
        parcel_id = self.local_store.crear_encomienda({
            "condo_id": self.config_mgr.condo_id,
            "condo_name": self.config_mgr.condo_name,
            "unit": self.datos_flujo["unidad_id"],
            "resident_name": residente.get("nombre", ""),
            "resident_user_id": residente.get("uid", ""),
            "tamano": recurso["tamano"],
            "locker_id": recurso["id"],
            "tipo_recurso": recurso["tipo"],
            "courier": self.datos_flujo.get("courier", ""),
            "created_by_name": "Kiosco",
        })

        # 3c. Disparar sincronización en segundo plano (no bloquea la GUI).
        self.sync.sincronizar_ahora()

        # 3d. Confirmación. El QR aparece en la app del residente (valor = parcel_id).
        courier = self.datos_flujo.get("courier", "")
        self._pantalla_resultado(
            titulo="¡Encomienda depositada!",
            mensaje=(f"Destinatario: {residente.get('nombre', '')}\n"
                     + (f"Empresa: {courier}\n" if courier else "")
                     + f"Casillero: {recurso['id']}\n\n"
                     f"El destinatario verá el código QR de retiro en su app."),
            color=COLOR_OK,
        )

    # ================================================================== #
    # Flujo: Retirar Encomienda
    # ================================================================== #
    def iniciar_retirar(self):
        """Retiro por lector de QR: la encomienda se valida por su ID (= QR)."""
        self.datos_flujo = {"accion": "retirar"}
        self._pantalla_escaneo(
            titulo="Retirar Encomienda",
            on_confirmar=self._procesar_retiro,
        )

    def retirar_por_codigo(self, codigo: str) -> dict:
        """
        Lógica de retiro COMPARTIDA (pantalla exterior y lector automático).

        Busca la encomienda local pendiente por su ID (= valor del QR), abre la
        cerradura de retiro, la marca como retirada y dispara la sincronización.

        Devuelve: {"ok": bool, "motivo": "invalido"|"hardware", "recurso_id", "error"}
        No toca la GUI, por lo que es seguro llamarla desde el hilo del lector.
        """
        entrega = self.local_store.get_encomienda_pendiente_por_id(codigo)
        if entrega is None:
            return {"ok": False, "motivo": "invalido"}

        recurso_id = entrega.get("locker_id")
        try:
            self.hardware.abrir_cerradura(recurso_id, "retiro")
        except ValueError as e:
            return {"ok": False, "motivo": "hardware", "error": str(e)}

        self.local_store.marcar_retirada(entrega["parcel_id"])
        self.allocator.refrescar_ocupacion()
        self.sync.sincronizar_ahora()
        return {"ok": True, "recurso_id": recurso_id}

    def _procesar_retiro(self, codigo: str):
        """Retiro desde la PANTALLA exterior (Caso 1 y respaldo del Caso 2)."""
        if not codigo:
            return

        r = self.retirar_por_codigo(codigo)
        if not r["ok"]:
            if r["motivo"] == "hardware":
                self._pantalla_resultado("Error de hardware", r.get("error", ""), COLOR_ERROR)
            else:
                self._pantalla_resultado(
                    titulo="Código inválido",
                    mensaje=("No hay una encomienda pendiente con ese código.\n"
                             "Verifique el QR e intente nuevamente."),
                    color=COLOR_ERROR,
                )
            return

        self._pantalla_resultado(
            titulo="¡Encomienda entregada!",
            mensaje=(f"Casillero {r['recurso_id']} abierto.\n"
                     f"Retire su paquete y cierre la puerta."),
            color=COLOR_OK,
        )

    def _on_scan_retiro(self, codigo: str):
        """
        Retiro AUTOMÁTICO desde el lector dedicado, SIN pantalla (Caso 2).
        Corre en el hilo del ScannerListener: solo opera la puerta y registra.
        """
        r = self.retirar_por_codigo(codigo)
        if r["ok"]:
            logger.info("Retiro automático OK: casillero %s abierto.", r["recurso_id"])
        else:
            logger.info("Retiro automático rechazado (%s).", r.get("motivo"))

    def _abrir_remoto(self, locker_id: str, operacion: str):
        """
        Callback del CommandListener: abre una cerradura por orden remota del
        operador (override manual). Devuelve (ok, error). Solo opera la puerta;
        no cambia el estado de encomiendas.
        """
        try:
            self.hardware.abrir_cerradura(locker_id, operacion)
            logger.info("Apertura remota OK: %s (%s).", locker_id, operacion)
            return True, ""
        except ValueError as e:
            logger.warning("Apertura remota rechazada: %s", e)
            return False, str(e)

    # ================================================================== #
    # Pantalla de escaneo de QR (lector actúa como teclado + Enter)
    # ================================================================== #
    def _pantalla_escaneo(self, titulo, on_confirmar):
        self._limpiar()

        tk.Label(self.contenedor, text=titulo, font=self.f_titulo,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(40, 10))

        tk.Label(self.contenedor, text="📷", font=("Helvetica", 90),
                 bg=COLOR_FONDO, fg=COLOR_MARCA).pack(pady=10)

        tk.Label(self.contenedor,
                 text="Acerque el código QR de su app al lector",
                 font=self.f_texto, bg=COLOR_FONDO, fg=COLOR_TENUE).pack(pady=(0, 20))

        # El lector QR "teclea" el código en este campo y envía Enter.
        # Se auto-enfoca para capturar el escaneo sin tocar la pantalla.
        entrada_var = tk.StringVar()
        entry = tk.Entry(self.contenedor, textvariable=entrada_var,
                         font=("Helvetica", 18), justify="center", width=26,
                         relief="flat", bg="#FFFFFF", fg=COLOR_TEXTO,
                         insertbackground=COLOR_TEXTO)
        entry.pack(pady=8, ipady=8)
        entry.focus_set()

        # Al recibir Enter (fin de escaneo), procesar el código.
        entry.bind("<Return>", lambda e: on_confirmar(entrada_var.get().strip()))

        self._boton(self.contenedor, "← Cancelar", self.mostrar_principal,
                    color=COLOR_GRIS).pack(side="bottom", pady=15)

    # ================================================================== #
    # Pantalla de teclado numérico reutilizable
    # ================================================================== #
    def _pantalla_teclado(self, titulo, on_confirmar):
        self._limpiar()
        entrada_var = tk.StringVar()

        tk.Label(self.contenedor, text=titulo, font=self.f_titulo,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(25, 10))

        tk.Label(self.contenedor, textvariable=entrada_var, font=self.f_titulo,
                 bg="#ffffff", fg="#000000", width=12).pack(pady=10)

        teclado = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        teclado.pack()

        def agregar(c):
            entrada_var.set(entrada_var.get() + c)

        def borrar():
            entrada_var.set(entrada_var.get()[:-1])

        teclas = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "←", "0", "OK"]
        for i, t in enumerate(teclas):
            fila, col = divmod(i, 3)
            if t == "OK":
                cmd = lambda: on_confirmar(entrada_var.get().strip())
                color = COLOR_OK
            elif t == "←":
                cmd = borrar
                color = "#e67e22"
            else:
                cmd = lambda c=t: agregar(c)
                color = COLOR_ACENTO
            self._boton(teclado, t, cmd, color=color, width=5).grid(
                row=fila, column=col, padx=6, pady=6)

        self._boton(self.contenedor, "← Cancelar", self.mostrar_principal,
                    color=COLOR_GRIS).pack(side="bottom", pady=15)

    # ================================================================== #
    # Pantallas de resultado
    # ================================================================== #
    def _pantalla_resultado(self, titulo, mensaje, color, auto_retorno_seg=5):
        self._limpiar()
        tk.Label(self.contenedor, text=titulo, font=self.f_titulo,
                 bg=COLOR_FONDO, fg=color).pack(pady=(60, 20))
        tk.Label(self.contenedor, text=mensaje, font=self.f_texto,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO, justify="center").pack(pady=10)
        self._boton(self.contenedor, "Volver al inicio",
                    self.mostrar_principal).pack(pady=30)
        tk.Label(self.contenedor,
                 text=f"Volviendo al inicio en {auto_retorno_seg} segundos…",
                 font=self.f_pie, bg=COLOR_FONDO, fg=COLOR_TENUE).pack(side="bottom", pady=8)

        # Auto-retorno al inicio (se cancela si el usuario navega antes).
        self._auto_return_id = self.after(
            auto_retorno_seg * 1000, self.mostrar_principal)

    # ================================================================== #
    def _salir(self):
        try:
            if self.scanner is not None:
                self.scanner.detener()
            if self.command_listener is not None:
                self.command_listener.detener()
            self.sync.detener()
            self.local_store.close()
            self.hardware.cleanup()
        finally:
            self.destroy()


def main():
    try:
        app = PorteriaApp()
    except ConfigError as e:
        logger.error("Error de configuración: %s", e)
        raise SystemExit(1)

    app.mainloop()


if __name__ == "__main__":
    main()
