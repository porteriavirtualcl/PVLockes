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

import queue
import logging
import tkinter as tk
from tkinter import font as tkfont

from config_manager import ConfigManager, ConfigError
from hardware_manager import HardwareManager
from local_store import LocalStore
from firebase_service import FirebaseService, FirebaseNoDisponibleError
from sync_service import SyncService
from scanner_listener import ScannerListener
from qr_http_listener import QRHttpListener
from command_listener import CommandListener
from sip_service import SipService
from webrtc_call_service import WebRTCCallService
from ai_assistant_service import AIAssistantService
from camera_service import CameraService
from resource_allocator import ResourceAllocator, SinDisponibilidadError

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Paleta de colores — tema claro moderno, alineado al logo de marca.
COLOR_FONDO = "#FAF9F5"       # fondo hueso cálido (mockup app móvil)
COLOR_TARJETA = "#FFFFFF"     # tarjetas / paneles
COLOR_MARCA = "#29ABE2"       # azul de marca (del logo)
COLOR_MARCA_OSC = "#1B8FC4"   # azul de marca, tono presionado
COLOR_ACENTO = COLOR_MARCA    # alias para el resto de pantallas
COLOR_TEXTO = "#141413"       # texto principal casi-negro (títulos)
COLOR_BOTON_TEXTO = "#FFFFFF"  # texto sobre botones de color
COLOR_OK = "#27AE60"
COLOR_ERROR = "#E74C3C"
COLOR_MORADO = "#7C4DBC"      # botón "Retirar"
COLOR_TENUE = "#8A9BA8"       # texto secundario / muted
COLOR_GRIS = "#B0BEC9"        # botones neutros (cancelar)
COLOR_BORDE = "#E6E5F0"       # borde suave de tarjetas/teclas blancas
COLOR_TARJETA_GRIS = "#EFEDE6"  # tecla neutra (Borrar) sobre fondo hueso
COLOR_AVATAR_2 = "#7C4DBC"    # segundo color de avatar (alterna con la marca)

# Tipografía de marca. Cantarell (la misma del mockup/app) está instalada en el
# kiosco; en un PC de desarrollo sin ella, Tk cae a su fuente por defecto.
FUENTE = "Cantarell"


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

        # Lector de retiro por RED (ej. FondVision en modo HTTP): recibe los
        # escaneos por HTTP y dispara el mismo retiro que el lector USB.
        self.qr_red = None
        cfg_red = self.config_mgr.as_dict().get("retiro_automatico", {}).get("red", {})
        if cfg_red.get("habilitado", False):
            self.qr_red = QRHttpListener(
                on_scan=self._on_scan_retiro,
                puerto=int(cfg_red.get("puerto", 8080)),
                parametro=cfg_red.get("parametro", "cardid"),
            )
            self.qr_red.iniciar()

        # Apertura remota: listener de comandos del operador (tiempo real).
        self.command_listener = CommandListener(
            firebase=self.firebase,
            kiosk_id=self.config_mgr.kiosk_id,
            on_abrir=self._abrir_remoto,
        )
        self.command_listener.iniciar()

        # Llamada SIP a la central (opcional; solo si está habilitado en config).
        # El callback de estado se reprograma con after() para tocar Tkinter
        # de forma segura desde los hilos de pjsua2.
        self.sip = None
        # baresip corre en OTRO hilo y no puede tocar Tkinter directamente
        # ("main thread is not in main loop"). El hilo del SIP encola los estados
        # en esta cola thread-safe; el hilo de la GUI la drena con un poller.
        self._sip_estado_q = queue.Queue()
        if self.config_mgr.sip_habilitado:
            self.sip = SipService(
                self.config_mgr.sip_config,
                on_estado=lambda estado, detalle: self._sip_estado_q.put((estado, detalle)),
            )
            self.sip.iniciar()
            self.after(400, self._poll_sip_estado)

        # Llamada de AUDIO al residente por WebRTC (reutiliza el botón "Llamar").
        # Corre en su propio hilo; los estados se encolan y la GUI los drena con
        # un poller (thread-safe, igual patrón que el SIP).
        self.webrtc = None
        self._webrtc_estado_q = queue.Queue()
        if self.firebase is not None:
            self.webrtc = WebRTCCallService(
                self.firebase, self.config_mgr,
                on_estado=lambda estado, detalle: self._webrtc_estado_q.put((estado, detalle)),
            )
            self.after(400, self._poll_webrtc_estado)

        # Conserje IA de voz (PILOTO) para el flujo de dejar encomienda.
        # Corre en su propio hilo; estados/transcripción llegan a la GUI por
        # una cola thread-safe que un poller drena (igual patrón que WebRTC/SIP).
        self.ai = None
        self._ia_estado_q = queue.Queue()
        if self.config_mgr.asistente_ia_habilitado:
            ia_cfg = self.config_mgr.asistente_ia_config
            api_key = self._resolver_api_key_ia(ia_cfg)
            if api_key:
                self.ai = AIAssistantService(
                    api_key=api_key,
                    on_estado=lambda tipo, texto: self._ia_estado_q.put((tipo, texto)),
                    on_abrir_casillero=self._ia_abrir_casillero,
                    modelo=ia_cfg.get("modelo", "gemini-2.5-flash-native-audio-latest"),
                    idioma=ia_cfg.get("idioma", "es-US"),
                )
                if self.ai.disponible:
                    self.after(400, self._poll_ia_estado)
                else:
                    self.ai = None  # faltan dependencias (google-genai/sounddevice)

        # Recepcionista de VOZ del botón "Llamar" (deriva: residente / encomienda
        # / operador). Requiere el asistente IA disponible y llamada_ia.habilitado.
        self.ai_recep = None
        self._transferir = None      # 'residente' | 'operador' (pendiente al cerrar la IA)
        cfg_llia = self.config_mgr.as_dict().get("llamada_ia", {})
        if self.ai is not None and bool(cfg_llia.get("habilitado", False)):
            ia_cfg = self.config_mgr.asistente_ia_config
            self.ai_recep = AIAssistantService(
                api_key=self._resolver_api_key_ia(ia_cfg),
                on_estado=lambda tipo, texto: self._ia_estado_q.put((tipo, texto)),
                on_accion=self._ia_accion_recepcion,
                modelo=ia_cfg.get("modelo", "gemini-2.5-flash-native-audio-latest"),
                idioma=ia_cfg.get("idioma", "es-US"),
                modo="recepcion",
            )
            if not self.ai_recep.disponible:
                self.ai_recep = None

        # Cámara USB (preview en la pantalla principal, junto al logo).
        self.camera = None
        if self.config_mgr.camara_habilitada:
            cam_cfg = self.config_mgr.camara_config
            cam = CameraService(dispositivo=cam_cfg.get("dispositivo", "/dev/video0"))
            if cam.disponible:
                cam.iniciar()
                self.camera = cam

        # --- Estado del flujo en curso ---
        self.datos_flujo = {}
        # Id del temporizador de auto-retorno al inicio (pantallas de resultado).
        self._auto_return_id = None
        # Inactividad: fuera de la pantalla principal, si nadie toca la pantalla
        # por estos segundos, se vuelve solo al inicio (deja el kiosco listo
        # para el próximo). Es un único número, fácil de ajustar.
        self._inactividad_seg = 5
        self._inactividad_id = None
        self._en_principal = True

        # --- Ventana (orientación configurable: vertical u horizontal) ---
        self.vertical = self.config_mgr.es_vertical
        self.title(self.config_mgr.nombre_sistema)
        self.configure(bg=COLOR_FONDO)
        self.geometry("480x800" if self.vertical else "800x480")
        if self.config_mgr.pantalla_completa:
            # Pantalla completa (kiosco). Se fija DESPUÉS del geometry y se
            # refuerza tras el mapeo, porque compositores Wayland/labwc a veces
            # ignoran el atributo si se establece demasiado temprano.
            self.attributes("-fullscreen", True)
            self.after(300, lambda: self.attributes("-fullscreen", True))
        self.bind("<Escape>", lambda e: self._salir())
        # Cualquier toque en la pantalla reinicia el contador de inactividad.
        self.bind_all("<Button-1>", self._reiniciar_inactividad, add="+")
        self.protocol("WM_DELETE_WINDOW", self._salir)

        # Fuentes — más compactas en vertical (480px de ancho) para evitar cortes.
        if self.vertical:
            self.f_titulo = tkfont.Font(family=FUENTE, size=22, weight="bold")
            self.f_boton = tkfont.Font(family=FUENTE, size=15, weight="bold")
            self.f_texto = tkfont.Font(family=FUENTE, size=13)
            self.f_pie = tkfont.Font(family=FUENTE, size=10)
        else:
            self.f_titulo = tkfont.Font(family=FUENTE, size=34, weight="bold")
            self.f_boton = tkfont.Font(family=FUENTE, size=20, weight="bold")
            self.f_texto = tkfont.Font(family=FUENTE, size=16)
            self.f_pie = tkfont.Font(family=FUENTE, size=11)

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
        # Toda pantalla que se arma se considera "no principal" y arranca el
        # contador de inactividad; mostrar_principal lo apaga al final.
        self._en_principal = False
        self._armar_inactividad()
        # Pausa el preview de cámara al salir de una pantalla (se reactiva en
        # la principal). Evita que decodifique durante llamadas/asistente IA.
        if getattr(self, "camera", None) is not None:
            self.camera.set_wanted(False)
        for widget in self.contenedor.winfo_children():
            widget.destroy()

    def _armar_inactividad(self):
        """(Re)programa el retorno a la principal por inactividad."""
        if self._inactividad_id is not None:
            self.after_cancel(self._inactividad_id)
            self._inactividad_id = None
        self._inactividad_id = self.after(
            self._inactividad_seg * 1000, self._volver_por_inactividad)

    def _reiniciar_inactividad(self, event=None):
        """Cada toque reinicia el contador (solo fuera de la principal)."""
        if not self._en_principal:
            self._armar_inactividad()

    def _volver_por_inactividad(self):
        self._inactividad_id = None
        if not self._en_principal:
            self.mostrar_principal()

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
        # Todo se escala respecto del alto base (190 px) para que la tarjeta
        # se vea proporcionada aunque se la agrande.
        s = h / 190.0
        r = int(26 * s)
        cv = tk.Canvas(parent, width=w, height=h, bg=COLOR_FONDO,
                       highlightthickness=0, cursor="hand2")
        self._rect_redondeado(cv, 2, 2, w - 2, h - 2, r, fill=color, outline=color)
        # Fuentes con tope, para que un título largo ("Dejar Encomienda") no se
        # desborde a lo ancho cuando la tarjeta es alta.
        f_emoji = min(int(44 * s), 60)
        f_tit = min(int(20 * s), 30)
        f_sub = min(int(12 * s), 16)
        cv.create_text(w / 2, 55 * s, text=emoji,
                       font=(FUENTE, f_emoji), fill=COLOR_BOTON_TEXTO)
        cv.create_text(w / 2, 118 * s, text=titulo,
                       font=(FUENTE, f_tit, "bold"), fill=COLOR_BOTON_TEXTO)
        cv.create_text(w / 2, 150 * s, text=subtitulo,
                       font=(FUENTE, f_sub), fill=COLOR_BOTON_TEXTO)
        cv.bind("<Button-1>", lambda e: comando())
        return cv

    def _btn_redondo(self, parent, texto, comando, w, h, fill, fg,
                     font, r=None, borde=None):
        """Botón/tecla rectangular de esquinas redondeadas (Canvas)."""
        r = r if r is not None else int(min(w, h) * 0.3)
        cv = tk.Canvas(parent, width=w, height=h, bg=COLOR_FONDO,
                       highlightthickness=0, cursor="hand2")
        self._rect_redondeado(cv, 2, 2, w - 2, h - 2, r,
                              fill=fill, outline=borde or fill)
        cv.create_text(w / 2, h / 2, text=texto, font=font, fill=fg)
        if comando is not None:
            cv.bind("<Button-1>", lambda e: comando())
        return cv

    def _encabezado(self, titulo, on_volver=None, sub=None):
        """Barra superior estilo app: botón redondo de volver + título."""
        barra = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        barra.pack(fill="x", padx=22, pady=(22, 6))
        if on_volver is not None:
            self._btn_redondo(
                barra, "‹", on_volver, w=46, h=46,
                fill="#FFFFFF", fg=COLOR_MARCA,
                font=(FUENTE, 24, "bold"), r=16, borde=COLOR_BORDE,
            ).pack(side="left")
        tk.Label(barra, text=titulo, font=(FUENTE, 21, "bold"),
                 bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(side="left", padx=12)
        if sub:
            tk.Label(self.contenedor, text=sub, font=(FUENTE, 13),
                     bg=COLOR_FONDO, fg=COLOR_TENUE).pack(pady=(0, 4))

    def _pie_hint(self, texto):
        """Texto guía discreto al pie de una pantalla del flujo."""
        tk.Label(self.contenedor, text=texto, font=(FUENTE, 12),
                 bg=COLOR_FONDO, fg=COLOR_TENUE).pack(side="bottom", pady=16)

    # ================================================================== #
    # Pantalla principal
    # ================================================================== #
    def mostrar_principal(self):
        self._limpiar()
        self.datos_flujo = {}

        # Cabecera: logo + (opcional) preview de la cámara USB a un costado.
        con_cam = getattr(self, "camera", None) is not None
        self._logo_img = self._cargar_logo(alto_px=110 if con_cam else 150)
        header = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        header.pack(pady=(22 if con_cam else 30, 6))
        if self._logo_img is not None:
            tk.Label(header, image=self._logo_img, bg=COLOR_FONDO).pack(
                side="left", padx=(0, 12 if con_cam else 0))
        else:
            tk.Label(header, text=self.config_mgr.nombre_sistema, font=self.f_titulo,
                     bg=COLOR_FONDO, fg=COLOR_MARCA).pack(side="left", padx=(0, 12 if con_cam else 0))
        if con_cam:
            cam_box = tk.Frame(header, bg="#0b1220", width=160, height=120,
                               highlightthickness=1, highlightbackground=COLOR_MARCA)
            cam_box.pack(side="left")
            cam_box.pack_propagate(False)
            self._cam_label = tk.Label(cam_box, bg="#0b1220")
            self._cam_label.pack(fill="both", expand=True)
            self.camera.set_wanted(True)      # reactiva la captura en la principal
            self._actualizar_camara()

        tk.Label(self.contenedor, text=self.config_mgr.condo_name,
                 font=(FUENTE, 22, "bold"), bg=COLOR_FONDO,
                 fg=COLOR_TEXTO).pack(pady=(0, 24))

        # Tarjetas de acción: apiladas en vertical, lado a lado en horizontal.
        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(pady=10)

        # Esta instalación opera SOLO como buzón de depósito: la pantalla
        # muestra únicamente "Dejar Encomienda". El retiro se hace por el lector
        # QR dedicado (retiro_automatico), no por pantalla.
        tarjetas = [
            ("📦", "Dejar Encomienda", "Toca para comenzar", self.iniciar_dejar, COLOR_MARCA),
        ]

        # Una sola tarjeta -> 1 columna y el botón ocupa el ancho útil.
        cols = 1
        if self.vertical:
            ancho, alto = 420, 340
            padx, pady = 0, 10
        else:
            # En 800x480 el alto es el recurso escaso: se crece a lo ancho.
            ancho, alto = 620, 210
            padx, pady = 0, 10
        for c in range(cols):
            marco.columnconfigure(c, weight=1)
        for i, (emoji, tit, sub, cmd, color) in enumerate(tarjetas):
            card = self._boton_tarjeta(marco, emoji, tit, sub, cmd, color, w=ancho, h=alto)
            fila, col = divmod(i, cols)
            card.grid(row=fila, column=col, padx=padx, pady=pady)

        self._pie_estado()

        # Ya estamos en la principal: apaga el contador de inactividad
        # (_limpiar lo dejó armado al entrar).
        self._en_principal = True
        if self._inactividad_id is not None:
            self.after_cancel(self._inactividad_id)
            self._inactividad_id = None

    def _pie_estado(self):
        """Muestra un indicador discreto de conexión y pendientes de sync."""
        pendientes = self.local_store.contar_pendientes_sync()
        online = self.firebase is not None and self.firebase.conectado
        estado = "🟢 En línea" if online else "🟠 Sin conexión (operando offline)"
        if pendientes:
            estado += f"  ·  {pendientes} por sincronizar"
        tk.Label(self.contenedor, text=estado, font=self.f_pie,
                 bg=COLOR_FONDO, fg=COLOR_TENUE).pack(side="bottom", pady=8)

    def _actualizar_camara(self):
        """Pinta el último frame de la cámara en el preview (hilo de la GUI)."""
        lbl = getattr(self, "_cam_label", None)
        if self.camera is None or lbl is None or not lbl.winfo_exists():
            return  # se salió de la pantalla principal -> deja de refrescar
        try:
            from PIL import ImageTk
            img = self.camera.latest()
            if img is not None:
                photo = ImageTk.PhotoImage(img)
                lbl.configure(image=photo)
                lbl._imgref = photo   # mantener referencia (si no, se ve negro)
        except Exception:  # noqa: BLE001
            pass
        self.after(130, self._actualizar_camara)   # ~8 fps

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

    @staticmethod
    def _iniciales(nombre: str) -> str:
        partes = [p for p in str(nombre).split() if p]
        if not partes:
            return "?"
        if len(partes) == 1:
            return partes[0][:2].upper()
        return (partes[0][0] + partes[1][0]).upper()

    def _fila_residente(self, parent, nombre, subtitulo, color_avatar, cmd,
                        w=420, h=74):
        """Fila blanca redondeada: avatar con iniciales + nombre + chevron."""
        cv = tk.Canvas(parent, width=w, height=h, bg=COLOR_FONDO,
                       highlightthickness=0, cursor="hand2")
        self._rect_redondeado(cv, 2, 2, w - 2, h - 2, 20,
                              fill="#FFFFFF", outline=COLOR_BORDE)
        # Avatar circular con iniciales.
        cy = h / 2
        cv.create_oval(16, cy - 21, 58, cy + 21, fill=color_avatar, outline=color_avatar)
        cv.create_text(37, cy, text=self._iniciales(nombre),
                       font=(FUENTE, 16, "bold"), fill="#FFFFFF")
        # Nombre + subtítulo.
        cv.create_text(74, cy - 11, text=nombre, anchor="w",
                       font=(FUENTE, 17, "bold"), fill=COLOR_TEXTO)
        cv.create_text(74, cy + 12, text=subtitulo, anchor="w",
                       font=(FUENTE, 12), fill=COLOR_TENUE)
        cv.create_text(w - 22, cy, text="›", font=(FUENTE, 24, "bold"),
                       fill=COLOR_TENUE)
        cv.bind("<Button-1>", lambda e: cmd())
        return cv

    def _pantalla_nombres(self, unidad_id: str, residentes: list):
        """Paso 1c: mostrar los nombres disponibles; el repartidor elige uno."""
        self._limpiar()
        etiqueta = self.config_mgr.etiqueta_unidad

        self._encabezado("¿Para quién es?", on_volver=self.iniciar_dejar)

        # Pill del departamento.
        pill = tk.Canvas(self.contenedor, width=230, height=38, bg=COLOR_FONDO,
                         highlightthickness=0)
        pill.pack(pady=(2, 14))
        self._rect_redondeado(pill, 2, 2, 228, 36, 18,
                              fill="#E3F2FB", outline="#E3F2FB")
        pill.create_text(115, 19, text=f"🏠  {etiqueta} {unidad_id}",
                         font=(FUENTE, 13, "bold"), fill=COLOR_MARCA_OSC)

        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(fill="both", expand=True, padx=30)

        for i, residente in enumerate(residentes):
            nombre = residente.get("nombre", "Sin nombre")
            color = COLOR_MARCA if i % 2 == 0 else COLOR_AVATAR_2
            self._fila_residente(
                marco, nombre, f"{etiqueta} {unidad_id}", color,
                lambda r=residente: self._seleccionar_residente(r),
            ).pack(pady=6)

        self._pie_hint("Toca el nombre del destinatario")

    def _seleccionar_residente(self, residente: dict):
        """El repartidor eligió un nombre: elegir la empresa de reparto."""
        self.datos_flujo["residente"] = residente
        self._pantalla_courier()

    def _pantalla_courier(self):
        """Paso: elegir la empresa de reparto (courier)."""
        self._limpiar()
        self._encabezado("Empresa de reparto", on_volver=self.iniciar_dejar)

        marco = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        marco.pack(pady=(6, 0))

        # Couriers administrados en el backend (Firestore, cacheados localmente).
        # Fallback a la lista de config.json si aún no se ha sincronizado.
        couriers = self.local_store.get_couriers() or self.config_mgr.couriers

        cols = 2
        for i, nombre in enumerate(couriers):
            fila, col = divmod(i, cols)
            self._btn_redondo(
                marco, nombre, lambda x=nombre: self._seleccionar_courier(x),
                w=196, h=58, fill="#FFFFFF", fg=COLOR_TEXTO,
                font=(FUENTE, 15, "bold"), r=18, borde=COLOR_BORDE,
            ).grid(row=fila, column=col, padx=7, pady=7)

        self._pie_hint("Toca la empresa que hace la entrega")

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

        # 3a. Accionar EN PARALELO la cerradura de depósito y la puerta de la
        #     sala: el repartidor entra y encuentra el casillero ya abierto.
        try:
            self.hardware.abrir_con_acceso_sala(recurso["id"], "deposito")
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

        # 3d. Confirmación con instrucciones GRANDES para el repartidor. La
        #     pantalla queda un buen rato porque la puerta de la sala está
        #     abierta ~60 s y el repartidor tiene que ingresar y depositar.
        self._pantalla_deposito_abierto(recurso["id"])

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
        codigo = (codigo or "").strip()

        # 1) Encomiendas depositadas EN este kiosco (base local, offline-capable).
        entrega = self.local_store.get_encomienda_pendiente_por_id(codigo)
        if entrega is not None:
            recurso_id = entrega.get("locker_id")
            try:
                self.hardware.abrir_cerradura(recurso_id, "retiro")
            except ValueError as e:
                return {"ok": False, "motivo": "hardware", "error": str(e)}
            self.local_store.marcar_retirada(entrega["parcel_id"])
            self.allocator.refrescar_ocupacion()
            self.sync.sincronizar_ahora()
            return {"ok": True, "recurso_id": recurso_id}

        # 2) Fallback: encomiendas creadas fuera del kiosco (app/operador) → Firestore.
        if self.firebase is not None:
            try:
                p = self.firebase.obtener_parcel(self.config_mgr.condo_id, codigo)
            except FirebaseNoDisponibleError:
                p = None
            if p and p.get("status") == "pending" and p.get("lockerId"):
                recurso_id = p["lockerId"]
                try:
                    self.hardware.abrir_cerradura(recurso_id, "retiro")
                except ValueError as e:
                    return {"ok": False, "motivo": "hardware", "error": str(e)}
                try:
                    import datetime
                    ahora = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    self.firebase.actualizar_parcel(
                        self.config_mgr.condo_id, codigo,
                        {"status": "picked_up", "picked_up_at": ahora})
                except FirebaseNoDisponibleError:
                    logger.warning("Retiro: no se pudo marcar %s como retirada en Firebase.", codigo)
                self.allocator.refrescar_ocupacion()
                return {"ok": True, "recurso_id": recurso_id}

        return {"ok": False, "motivo": "invalido"}

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

    def _abrir_remoto(self, locker_id: str, operacion: str, accion: str = "abrir"):
        """
        Callback del CommandListener: acciona puertas por orden remota (override
        manual del operador / super administrador). Devuelve (ok, error). Solo
        opera puertas; no cambia el estado de encomiendas.

        Acciones:
          - "abrir"       -> una cerradura (locker_id + operacion).
          - "abrir_sala"  -> libera la puerta de acceso a la sala.
          - "abrir_todas" -> todas las cerraduras del equipo, ambos lados.
        """
        try:
            if accion == "abrir_sala":
                lanzada = self.hardware.abrir_puerta_sala()
                if lanzada:
                    logger.info("Apertura remota OK: puerta de sala.")
                    return True, ""
                return False, "puerta de sala deshabilitada o ya liberada"

            if accion == "abrir_todas":
                ok, errores = self.hardware.abrir_todas()
                logger.info("Apertura remota masiva: %s abiertas, %s error(es).",
                            ok, len(errores))
                return (len(errores) == 0), "; ".join(errores)

            # Acción por defecto: una sola cerradura.
            self.hardware.abrir_cerradura(locker_id, operacion)
            logger.info("Apertura remota OK: %s (%s).", locker_id, operacion)
            return True, ""
        except ValueError as e:
            logger.warning("Apertura remota rechazada: %s", e)
            return False, str(e)

    # ================================================================== #
    # Llamada SIP a la central (conserjería / centro de monitoreo)
    # ================================================================== #
    # Textos amigables para cada estado que informa el SipService.
    _TEXTO_ESTADO_SIP = {
        "inicializando": "Preparando llamada…",
        "registrando": "Conectando con la central…",
        "registrado": "Listo para llamar",
        "error_registro": "No se pudo conectar con la central",
        "llamando": "Llamando…",
        "timbrando": "Timbrando…",
        "en_llamada": "En llamada",
        "colgado": "Llamada finalizada",
        "fallo_llamada": "No se pudo establecer la llamada",
        "no_disponible": "Llamada no disponible",
    }

    def iniciar_llamada(self):
        """Botón 'Llamar': recepcionista IA si está habilitada; si no, SIP directo; si no, WebRTC."""
        if self.ai_recep is not None and self.sip is not None and self.sip.disponible:
            self._transferir = None
            self._ia_buf = ""
            self._pantalla_asistente_ia(titulo="Portería Virtual", svc=self.ai_recep)
            self.ai_recep.iniciar()
            return
        if self.sip is not None and self.sip.disponible:
            numero = self.config_mgr.sip_config.get("destino", "")
            self._pantalla_llamada_sip(numero)
            self.sip.llamar()
            return
        if self.webrtc is not None:
            destino = self._destino_llamada()
            self._pantalla_llamada(destino.get("nombre", "Residente"))
            self.webrtc.llamar(destino)

    def _ia_accion_recepcion(self, nombre: str, args: dict) -> dict:
        """Acciones del recepcionista IA (corre en el hilo del asistente; NO tocar Tk).
        Solo deja anotada la transferencia; la GUI marca por SIP cuando la IA
        termina y libera el audio (estado 'finalizado')."""
        if nombre == "comunicar_residente":
            nombre_v = (args.get("nombre_visitante") or "").strip()
            self._transferir = {"tipo": "residente", "nombre": nombre_v,
                                "motivo": (args.get("motivo") or "").strip()}
            # Generar el anuncio TTS YA (mientras la IA se despide, ~7 s):
            # así el WAV está listo cuando el residente conteste.
            if nombre_v:
                self._transferir["anuncio"] = self._generar_anuncio_async(nombre_v)
            return {"ok": True, "mensaje": "Transfiriendo con el residente."}
        if nombre == "llamar_operador":
            self._transferir = {"tipo": "operador"}
            return {"ok": True, "mensaje": "Transfiriendo con el operador."}
        return {"ok": False, "mensaje": "Acción desconocida."}

    def _generar_anuncio_async(self, nombre_v: str) -> str:
        """Lanza la generación del anuncio TTS en segundo plano; devuelve la ruta."""
        import os
        import threading as _th
        ruta = "/tmp/anuncio_porteria.wav"
        try:
            if os.path.exists(ruta):
                os.remove(ruta)   # no reproducir un anuncio viejo
        except OSError:
            pass
        texto = (f"Hola. Tiene una llamada desde la portería del edificio, "
                 f"de parte de {nombre_v}. Le comunicamos.")
        from ai_assistant_service import generar_anuncio_tts
        ia_cfg = self.config_mgr.asistente_ia_config
        api_key = self._resolver_api_key_ia(ia_cfg)
        _th.Thread(target=generar_anuncio_tts,
                   args=(api_key, texto, ruta), daemon=True).start()
        return ruta

    def _ejecutar_transferencia(self):
        """Marca por SIP la transferencia pendiente (hilo de la GUI, IA ya cerrada)."""
        datos, self._transferir = self._transferir, None
        if not isinstance(datos, dict):
            datos = {"tipo": str(datos)}
        cfg_llia = self.config_mgr.as_dict().get("llamada_ia", {})
        if datos.get("tipo") == "operador":
            numero = cfg_llia.get("operador", "").strip()
            anuncio = None
        else:
            numero = self.config_mgr.sip_config.get("destino", "").strip()
            anuncio = datos.get("anuncio")   # WAV ya en generación desde que la IA tomó el nombre
        if not numero or self.sip is None:
            self.mostrar_principal()
            return
        self._pantalla_llamada_sip(numero)
        self.sip.llamar(numero, anuncio_wav=anuncio)

    def _pantalla_llamada_sip(self, numero: str = ""):
        self._limpiar()
        tk.Label(self.contenedor, text="Llamando a", font=self.f_texto,
                 bg=COLOR_FONDO, fg=COLOR_TENUE).pack(pady=(50, 0))
        tk.Label(self.contenedor, text=numero or "Central", font=self.f_titulo,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO, wraplength=440).pack(pady=(0, 10))
        tk.Label(self.contenedor, text="📞", font=(FUENTE, 90),
                 bg=COLOR_FONDO, fg=COLOR_OK).pack(pady=10)
        self._lbl_estado_sip = tk.Label(self.contenedor, text="Llamando…",
                                         font=self.f_texto, bg=COLOR_FONDO, fg=COLOR_TENUE)
        self._lbl_estado_sip.pack(pady=(0, 30))
        self._boton(self.contenedor, "🔴 Colgar", self._colgar_sip,
                    color=COLOR_ERROR).pack(pady=10)

    def _colgar_sip(self):
        if self.sip is not None:
            self.sip.colgar()
        self.mostrar_principal()

    def _destino_llamada(self) -> dict:
        """Residente a llamar. Configurable en config.json -> 'llamada'."""
        ll = self.config_mgr.as_dict().get("llamada", {})
        return {"uid": ll.get("destino_uid", ""),
                "nombre": ll.get("destino_nombre", "Residente")}

    def _pantalla_llamada(self, nombre: str = "Residente"):
        self._limpiar()
        tk.Label(self.contenedor, text="Llamando a", font=self.f_texto,
                 bg=COLOR_FONDO, fg=COLOR_TENUE).pack(pady=(50, 0))
        tk.Label(self.contenedor, text=nombre, font=self.f_titulo,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO, wraplength=440).pack(pady=(0, 10))
        tk.Label(self.contenedor, text="📞", font=(FUENTE, 90),
                 bg=COLOR_FONDO, fg=COLOR_OK).pack(pady=10)

        # Etiqueta de estado (se actualiza desde _on_estado_webrtc).
        self._lbl_estado_llamada = tk.Label(
            self.contenedor, text="Llamando…", font=self.f_texto,
            bg=COLOR_FONDO, fg=COLOR_TENUE)
        self._lbl_estado_llamada.pack(pady=(0, 30))

        self._boton(self.contenedor, "🔴 Colgar", self._colgar_llamada,
                    color=COLOR_ERROR).pack(pady=10)

    def _colgar_llamada(self):
        if self.webrtc is not None:
            self.webrtc.colgar()
        self.mostrar_principal()

    # Textos amigables para los estados de la llamada WebRTC.
    _TEXTO_ESTADO_LLAMADA = {
        "preparando": "Preparando llamada…",
        "timbrando": "Llamando… (esperando que contesten)",
        "en_llamada": "En llamada",
        "colgado": "Llamada finalizada",
        "finalizado": "Llamada finalizada",
        "sin_respuesta": "No contestaron",
        "rechazada": "Llamada rechazada",
        "fallo": "No se pudo llamar",
        "no_disponible": "Llamada no disponible",
    }

    def _poll_webrtc_estado(self):
        """Drena la cola de estados de la llamada WebRTC (hilo de la GUI)."""
        try:
            while True:
                estado, detalle = self._webrtc_estado_q.get_nowait()
                self._on_estado_webrtc(estado, detalle)
        except queue.Empty:
            pass
        self.after(300, self._poll_webrtc_estado)

    def _on_estado_webrtc(self, estado: str, detalle: str = ""):
        """Estado de la llamada WebRTC (drenado en el hilo de la GUI)."""
        texto = self._TEXTO_ESTADO_LLAMADA.get(estado, estado)
        lbl = getattr(self, "_lbl_estado_llamada", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.config(text=texto)
            if estado in ("colgado", "finalizado", "sin_respuesta", "rechazada", "fallo"):
                self.after(2500, self.mostrar_principal)

    def _poll_sip_estado(self):
        """Drena la cola de estados del SIP en el hilo de la GUI (thread-safe)."""
        try:
            while True:
                estado, detalle = self._sip_estado_q.get_nowait()
                self._on_estado_sip(estado, detalle)
        except queue.Empty:
            pass
        self.after(300, self._poll_sip_estado)

    def _on_estado_sip(self, estado: str, detalle: str = ""):
        """Estado del SIP (drenado desde la cola, en el hilo de la GUI)."""
        texto = self._TEXTO_ESTADO_SIP.get(estado, estado)
        lbl = getattr(self, "_lbl_estado_sip", None)
        # Solo actualiza si la pantalla de llamada sigue visible.
        if lbl is not None and lbl.winfo_exists():
            lbl.config(text=texto)
            # Si la llamada terminó/falló, volver al inicio tras un momento.
            if estado in ("colgado", "fallo_llamada", "error_registro"):
                self.after(2500, self.mostrar_principal)

    # ================================================================== #
    # Conserje IA de voz (PILOTO) — atiende al repartidor para dejar encomienda
    # ================================================================== #
    @staticmethod
    def _resolver_api_key_ia(ia_cfg: dict) -> str:
        """API key de Gemini: config -> archivo (api_key_file) -> variable de entorno."""
        import os
        key = (ia_cfg.get("api_key") or "").strip()
        if not key:
            ruta = ia_cfg.get("api_key_file") or ""
            ruta = os.path.expanduser(ruta) if ruta else ""
            if ruta and os.path.exists(ruta):
                try:
                    with open(ruta, "r", encoding="utf-8") as f:
                        key = f.read().strip()
                except OSError:
                    key = ""
        if not key:
            key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        return key

    def iniciar_dejar_ia(self):
        """Botón 'Dejar con Asistente IA': inicia el diálogo de voz."""
        if self.ai is None:
            return
        self._ia_buf = ""
        self._pantalla_asistente_ia()
        self.ai.iniciar()

    def _pantalla_asistente_ia(self, titulo="Asistente de encomiendas", svc=None):
        self._ia_activa = svc or self.ai
        self._limpiar()
        tk.Label(self.contenedor, text=titulo,
                 font=self.f_titulo, bg=COLOR_FONDO, fg=COLOR_TEXTO,
                 wraplength=440).pack(pady=(28, 2))
        tk.Label(self.contenedor, text="🎙️", font=(FUENTE, 78),
                 bg=COLOR_FONDO, fg="#16A085").pack(pady=6)
        self._lbl_estado_ia = tk.Label(self.contenedor, text="Conectando…",
                                        font=self.f_texto, bg=COLOR_FONDO, fg=COLOR_TENUE)
        self._lbl_estado_ia.pack(pady=(0, 22))
        # (No se muestra la transcripción de la conversación en pantalla.)
        self._boton(self.contenedor, "🔴 Terminar", self._terminar_ia,
                    color=COLOR_ERROR).pack(pady=8)

    def _terminar_ia(self):
        svc = getattr(self, "_ia_activa", None) or self.ai
        if svc is not None:
            svc.terminar()
        self._transferir = None   # cortar manualmente cancela la transferencia
        self.mostrar_principal()

    _TEXTO_ESTADO_IA = {
        "conectando": "Conectando…",
        "en_conversacion": "Escuchando… hable con el asistente",
        "reconectando": "Reconectando…",
        "abriendo_casillero": "Abriendo casillero…",
        "casillero_abierto": "¡Casillero abierto!",
        "transfiriendo": "Transfiriendo su llamada…",
        "finalizado": "Conversación finalizada",
        "error": "Asistente no disponible",
    }

    def _poll_ia_estado(self):
        """Drena la cola de estados del asistente IA (hilo de la GUI)."""
        try:
            while True:
                tipo, texto = self._ia_estado_q.get_nowait()
                self._on_estado_ia(tipo, texto)
        except queue.Empty:
            pass
        self.after(300, self._poll_ia_estado)

    def _on_estado_ia(self, tipo: str, texto: str = ""):
        lbl_e = getattr(self, "_lbl_estado_ia", None)
        vivo_e = lbl_e is not None and lbl_e.winfo_exists()

        # Solo se refleja el ESTADO (no la transcripción de la conversación).
        if tipo in self._TEXTO_ESTADO_IA and vivo_e:
            lbl_e.config(text=self._TEXTO_ESTADO_IA[tipo])

        if tipo == "casillero_abierto":
            if vivo_e:
                lbl_e.config(text=f"¡Casillero {texto} abierto!  Deje el paquete y cierre la puerta.")
            # Volver al inicio tras la despedida (la sesión se auto-cierra sola).
            self.after(11000, self.mostrar_principal)

        if tipo == "finalizado" and self._transferir:
            # La IA cerró y liberó el audio: marcar por SIP la transferencia.
            self._ejecutar_transferencia()
            return

        if tipo in ("finalizado", "error") and vivo_e:
            self.after(2500, self.mostrar_principal)

    def _ia_abrir_casillero(self, datos: dict) -> dict:
        """
        Callback REAL invocado por el asistente IA (corre en su hilo; LocalStore
        es thread-safe). Asigna un locker libre, acciona la cerradura de depósito
        y registra la encomienda local. Devuelve el resultado para el modelo.
        """
        nombre = (datos.get("nombre_repartidor") or "").strip()
        empresa = (datos.get("empresa") or "").strip()
        unidad = (datos.get("unidad") or "").strip()

        # 1) Asignar un locker (las encomiendas del piloto van a locker, no buzón).
        try:
            recurso = self.allocator.asignar("mediana")
        except (SinDisponibilidadError, ValueError) as e:
            logger.warning("IA: sin casillero disponible: %s", e)
            return {"ok": False, "mensaje": "No hay casilleros disponibles en este momento."}

        # 2) Cerradura de depósito + puerta de la sala, en paralelo.
        try:
            self.hardware.abrir_con_acceso_sala(recurso["id"], "deposito")
        except ValueError as e:
            self.allocator.liberar(recurso["id"])
            logger.error("IA: error de hardware al abrir %s: %s", recurso["id"], e)
            return {"ok": False, "mensaje": "No se pudo abrir el casillero."}

        # 3) Si la unidad es reconocible y tiene un único residente, vincularlo.
        resident_name, resident_uid = "", ""
        if unidad:
            residentes = self.local_store.get_residentes_por_unidad(unidad)
            if len(residentes) == 1:
                resident_name = residentes[0].get("nombre", "")
                resident_uid = residentes[0].get("uid", "")

        # 4) Registrar la encomienda local (el SyncService la empuja a Firebase).
        try:
            self.local_store.crear_encomienda({
                "condo_id": self.config_mgr.condo_id,
                "condo_name": self.config_mgr.condo_name,
                "unit": unidad,
                "resident_name": resident_name,
                "resident_user_id": resident_uid,
                "tamano": recurso.get("tamano", ""),
                "locker_id": recurso["id"],
                "tipo_recurso": recurso["tipo"],
                "courier": empresa,
                "created_by_name": f"Kiosco IA · {nombre}".strip(" ·"),
            })
        except Exception as e:  # noqa: BLE001
            logger.error("IA: error registrando encomienda: %s", e)
            # El casillero ya se abrió; no se revierte, pero se informa OK igual.

        logger.info("IA: casillero %s abierto para %s (%s) unidad '%s'",
                    recurso["id"], nombre, empresa, unidad)
        return {"ok": True, "casillero": recurso["id"],
                "mensaje": f"Casillero {recurso['id']} abierto."}

    # ================================================================== #
    # Pantalla de escaneo de QR (lector actúa como teclado + Enter)
    # ================================================================== #
    def _pantalla_escaneo(self, titulo, on_confirmar):
        self._limpiar()

        tk.Label(self.contenedor, text=titulo, font=self.f_titulo,
                 bg=COLOR_FONDO, fg=COLOR_TEXTO).pack(pady=(40, 10))

        tk.Label(self.contenedor, text="📷", font=(FUENTE, 90),
                 bg=COLOR_FONDO, fg=COLOR_MARCA).pack(pady=10)

        tk.Label(self.contenedor,
                 text="Acerque el código QR de su app al lector",
                 font=self.f_texto, bg=COLOR_FONDO, fg=COLOR_TENUE).pack(pady=(0, 20))

        # El lector QR "teclea" el código en este campo y envía Enter.
        # Se auto-enfoca para capturar el escaneo sin tocar la pantalla.
        entrada_var = tk.StringVar()
        entry = tk.Entry(self.contenedor, textvariable=entrada_var,
                         font=(FUENTE, 18), justify="center", width=26,
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
    def _pantalla_teclado(self, titulo, on_confirmar, etiqueta="DEPTO"):
        self._limpiar()
        entrada = {"v": ""}

        self._encabezado(titulo, on_volver=self.mostrar_principal)

        # Tarjeta blanca con el número escrito (DEPTO / 100).
        disp = tk.Canvas(self.contenedor, width=300, height=118, bg=COLOR_FONDO,
                         highlightthickness=0)
        disp.pack(pady=(6, 14))
        self._rect_redondeado(disp, 2, 2, 298, 116, 24,
                              fill="#FFFFFF", outline=COLOR_BORDE)
        disp.create_text(150, 34, text=etiqueta.upper(),
                         font=(FUENTE, 13, "bold"), fill=COLOR_TENUE)
        num_id = disp.create_text(150, 74, text="",
                                  font=(FUENTE, 40, "bold"), fill=COLOR_MARCA)

        def refrescar():
            disp.itemconfig(num_id, text=entrada["v"])

        def agregar(c):
            entrada["v"] += c
            refrescar()

        def borrar():
            entrada["v"] = entrada["v"][:-1]
            refrescar()

        teclado = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        teclado.pack()

        # 1-9, luego Borrar / 0 / → (flecha de confirmar).
        teclas = ["1", "2", "3", "4", "5", "6", "7", "8", "9",
                  "Borrar", "0", "→"]
        for i, t in enumerate(teclas):
            fila, col = divmod(i, 3)
            if t == "→":
                cmd = lambda: on_confirmar(entrada["v"].strip())
                key = self._btn_redondo(teclado, "→", cmd, w=96, h=72,
                                        fill=COLOR_MARCA, fg="#FFFFFF",
                                        font=(FUENTE, 30, "bold"), r=20)
            elif t == "Borrar":
                key = self._btn_redondo(teclado, "Borrar", borrar, w=96, h=72,
                                        fill=COLOR_TARJETA_GRIS, fg=COLOR_TENUE,
                                        font=(FUENTE, 16, "bold"), r=20)
            else:
                key = self._btn_redondo(teclado, t, lambda c=t: agregar(c),
                                        w=96, h=72, fill="#FFFFFF", fg=COLOR_TEXTO,
                                        font=(FUENTE, 26, "bold"), r=20,
                                        borde=COLOR_BORDE)
            key.grid(row=fila, column=col, padx=7, pady=7)

        self._pie_hint("Escribe el número y toca la flecha")

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

    def _pantalla_deposito_abierto(self, recurso_id, auto_retorno_seg=5):
        """
        Confirmación del depósito, con la instrucción EN GRANDE para que el
        repartidor la lea de pie mientras la puerta de la sala está abierta.

        El auto-retorno es largo (60 s por defecto) para que el mensaje siga
        visible durante todo el tiempo que la puerta de acceso está liberada.
        """
        self._limpiar()
        vertical = getattr(self, "vertical", True)

        # Bloque centrado verticalmente para que no se apile desde arriba ni se
        # corte abajo (la versión anterior recortaba el título a lo ancho).
        centro = tk.Frame(self.contenedor, bg=COLOR_FONDO)
        centro.pack(expand=True)

        # Círculo verde con check (como el mockup).
        d = 104
        cir = tk.Canvas(centro, width=d, height=d, bg=COLOR_FONDO,
                        highlightthickness=0)
        cir.pack(pady=(0, 18))
        cir.create_oval(6, 6, d - 6, d - 6, fill=COLOR_OK, outline=COLOR_OK)
        cir.create_text(d / 2, d / 2 + 2, text="✓",
                        font=(FUENTE, 54, "bold"), fill="#FFFFFF")

        # Título centrado y con wrap: nunca se corta a lo ancho.
        tk.Label(centro, text=f"Casillero {recurso_id} abierto",
                 font=(FUENTE, 30, "bold"), bg=COLOR_FONDO, fg=COLOR_OK,
                 wraplength=440, justify="center").pack(pady=(0, 18))

        # Tarjeta blanca con la instrucción, en grande.
        ancho = 430 if vertical else 620
        alto = 208
        card = tk.Canvas(centro, width=ancho, height=alto, bg=COLOR_FONDO,
                         highlightthickness=0)
        card.pack()
        self._rect_redondeado(card, 2, 2, ancho - 2, alto - 2, 24,
                              fill="#FFFFFF", outline=COLOR_BORDE)
        card.create_text(
            ancho / 2, alto / 2,
            text=("Se abrió la puerta del locker\ny de la sala.\n\n"
                  "Ingresa y deposita la encomienda.\n\n"
                  "Deja bien cerrado."),
            font=(FUENTE, 18, "bold"), fill=COLOR_TEXTO,
            justify="center", width=ancho - 48,
        )

        tk.Label(centro, text="¡Gracias!", font=(FUENTE, 22, "bold"),
                 bg=COLOR_FONDO, fg=COLOR_OK).pack(pady=(18, 0))

        # Sin botón: el mensaje se muestra unos segundos y vuelve solo al inicio.
        # (La puerta de la sala sigue abierta su tiempo completo aunque la
        # pantalla ya haya vuelto a la principal.)
        self._auto_return_id = self.after(
            auto_retorno_seg * 1000, self.mostrar_principal)

    # ================================================================== #
    def _salir(self):
        try:
            if self.scanner is not None:
                self.scanner.detener()
            if getattr(self, "qr_red", None) is not None:
                self.qr_red.detener()
            if self.command_listener is not None:
                self.command_listener.detener()
            if self.sip is not None:
                self.sip.detener()
            if self.webrtc is not None:
                self.webrtc.detener()
            if self.ai is not None:
                self.ai.detener()
            if self.ai_recep is not None:
                self.ai_recep.detener()
            if self.camera is not None:
                self.camera.detener()
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
