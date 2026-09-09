"""
scanner_listener.py
-------------------
Escucha un lector de QR DEDICADO (ej. el lado interior de lockers de doble
puerta) en un hilo de fondo, para el retiro automático SIN pantalla.

Por qué evdev:
    Un lector "headless" no puede depender del foco de una ventana. En Linux se
    lee el dispositivo de entrada directamente (/dev/input/by-id/...-event-kbd)
    con la librería `evdev`, de modo que:
      - funciona aunque la pantalla exterior esté en cualquier estado,
      - no se mezcla con las pulsaciones del lector/teclado de la pantalla.

Portabilidad:
    - En Linux/Raspberry usa `evdev` (requiere `pip install evdev` y permisos de
      lectura sobre el dispositivo: usuario en el grupo 'input' o udev rule).
    - En Windows/Mac (desarrollo) evdev no existe: el listener queda INACTIVO
      (no-op) y se registra por log. El retiro por pantalla sigue disponible.

El lector escribe el código (ej. el UUID del QR) y termina con Enter.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Mapa de teclas evdev -> carácter (suficiente para UUIDs hex y códigos alfanum.).
_KEYMAP = {
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4",
    "KEY_5": "5", "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9",
    "KEY_A": "a", "KEY_B": "b", "KEY_C": "c", "KEY_D": "d", "KEY_E": "e",
    "KEY_F": "f", "KEY_G": "g", "KEY_H": "h", "KEY_I": "i", "KEY_J": "j",
    "KEY_K": "k", "KEY_L": "l", "KEY_M": "m", "KEY_N": "n", "KEY_O": "o",
    "KEY_P": "p", "KEY_Q": "q", "KEY_R": "r", "KEY_S": "s", "KEY_T": "t",
    "KEY_U": "u", "KEY_V": "v", "KEY_W": "w", "KEY_X": "x", "KEY_Y": "y",
    "KEY_Z": "z", "KEY_MINUS": "-",
}
_TECLAS_ENTER = ("KEY_ENTER", "KEY_KPENTER")
_TECLAS_SHIFT = ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT")


class ScannerListener:
    """
    Lee un lector evdev dedicado y llama `on_scan(codigo)` por cada escaneo
    (código completo, al recibir Enter).
    """

    def __init__(self, dispositivo: str, on_scan):
        self.dispositivo = dispositivo
        self.on_scan = on_scan
        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None
        self._device = None

    # ------------------------------------------------------------------ #
    def iniciar(self) -> bool:
        """Abre el dispositivo y arranca el hilo. Devuelve True si quedó activo."""
        try:
            import evdev  # noqa: F401
        except ImportError:
            logger.warning(
                "Retiro automático: 'evdev' no disponible (¿Windows/dev?). "
                "El lector dedicado queda inactivo; use el retiro por pantalla."
            )
            return False

        # Resolver el dispositivo: si no hay uno configurado, o el configurado no
        # existe, se auto-detecta cualquier teclado HID (así un lector nuevo
        # funciona sin editar la config; su ruta by-id cambia según el modelo).
        import os
        dispositivo = self.dispositivo
        if not dispositivo or dispositivo == "auto" or not os.path.exists(dispositivo):
            if dispositivo and dispositivo != "auto":
                logger.info("Retiro automático: '%s' no existe; autodetectando lector…",
                            dispositivo)
            dispositivo = self._autodetectar()
            if not dispositivo:
                logger.info("Retiro automático: no se encontró ningún lector HID (inactivo).")
                return False
            logger.info("Retiro automático: lector autodetectado en %s", dispositivo)
        self.dispositivo = dispositivo

        try:
            from evdev import InputDevice
            self._device = InputDevice(self.dispositivo)
            # Tomar control exclusivo para que el escaneo no llegue también a la pantalla.
            try:
                self._device.grab()
            except OSError:
                logger.warning("No se pudo hacer grab() del lector; sigue en modo compartido.")
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.error("Retiro automático: no se pudo abrir '%s': %s", self.dispositivo, e)
            return False

        self._stop.clear()
        self._hilo = threading.Thread(target=self._loop, name="ScannerListener", daemon=True)
        self._hilo.start()
        logger.info("Retiro automático ACTIVO leyendo '%s'.", self.dispositivo)
        return True

    # ------------------------------------------------------------------ #
    @staticmethod
    def _autodetectar() -> str | None:
        """
        Busca un teclado HID (un lector QR se presenta como teclado). Elige el
        primer dispositivo que tenga ENTER + dígitos y NO sea táctil/mouse.
        Excluye la pantalla táctil (MPI7002) para no capturarla por error.
        """
        try:
            from evdev import InputDevice, list_devices, ecodes
        except ImportError:
            return None
        for path in list_devices():
            try:
                d = InputDevice(path)
            except Exception:  # noqa: BLE001
                continue
            try:
                caps = d.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                tiene_enter = ecodes.KEY_ENTER in keys
                tiene_digitos = ecodes.KEY_1 in keys and ecodes.KEY_0 in keys
                es_tactil = ecodes.EV_ABS in caps           # touchscreen/touchpad
                nombre = (d.name or "").lower()
                excluido = "mpi7002" in nombre or "touch" in nombre
                if tiene_enter and tiene_digitos and not es_tactil and not excluido:
                    return path
            finally:
                try:
                    d.close()
                except Exception:  # noqa: BLE001
                    pass
        return None

    # ------------------------------------------------------------------ #
    def _loop(self):
        from evdev import categorize, ecodes
        buffer = ""
        shift = False  # los IDs de Firestore distinguen may/min: hay que respetar shift
        try:
            for event in self._device.read_loop():
                if self._stop.is_set():
                    break
                if event.type != ecodes.EV_KEY:
                    continue
                data = categorize(event)

                keycode = data.keycode
                if isinstance(keycode, list):  # evdev puede devolver lista de alias
                    keycode = keycode[0]

                # El shift se rastrea en down y up (no es una "pulsación" que emita char).
                if keycode in _TECLAS_SHIFT:
                    shift = (data.keystate != data.key_up)
                    continue

                if data.keystate != data.key_down:  # el resto: solo pulsación (key down)
                    continue

                if keycode in _TECLAS_ENTER:
                    codigo = buffer.strip()
                    buffer = ""
                    if codigo:
                        self._disparar(codigo)
                elif keycode in _KEYMAP:
                    ch = _KEYMAP[keycode]
                    buffer += ch.upper() if shift and ch.isalpha() else ch
                # Otras teclas se ignoran.
        except OSError as e:
            logger.error("Retiro automático: lectura interrumpida: %s", e)
        finally:
            logger.info("ScannerListener finalizado.")

    def _disparar(self, codigo: str):
        logger.info("Retiro automático: código escaneado.")
        try:
            self.on_scan(codigo)
        except Exception as e:  # noqa: BLE001 - nunca dejar caer el hilo del lector
            logger.error("Error procesando retiro automático: %s", e)

    # ------------------------------------------------------------------ #
    def detener(self):
        self._stop.set()
        if self._device is not None:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001
                pass
        if self._hilo:
            self._hilo.join(timeout=2)
        logger.info("ScannerListener detenido.")
