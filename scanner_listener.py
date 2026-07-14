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
        if not self.dispositivo:
            logger.info("Retiro automático: sin dispositivo configurado (inactivo).")
            return False

        try:
            import evdev  # noqa: F401
        except ImportError:
            logger.warning(
                "Retiro automático: 'evdev' no disponible (¿Windows/dev?). "
                "El lector dedicado queda inactivo; use el retiro por pantalla."
            )
            return False

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
    def _loop(self):
        from evdev import categorize, ecodes
        buffer = ""
        try:
            for event in self._device.read_loop():
                if self._stop.is_set():
                    break
                if event.type != ecodes.EV_KEY:
                    continue
                data = categorize(event)
                if data.keystate != data.key_down:  # solo pulsación (key down)
                    continue

                keycode = data.keycode
                if isinstance(keycode, list):  # evdev puede devolver lista de alias
                    keycode = keycode[0]

                if keycode in _TECLAS_ENTER:
                    codigo = buffer.strip()
                    buffer = ""
                    if codigo:
                        self._disparar(codigo)
                elif keycode in _KEYMAP:
                    buffer += _KEYMAP[keycode]
                # Otras teclas (shift, etc.) se ignoran.
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
