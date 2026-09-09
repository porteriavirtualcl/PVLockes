"""
camera_service.py
-----------------
Preview en vivo de una cámara USB (UVC / v4l2) para mostrarlo en la pantalla
principal del kiosco, junto al logo.

Diseño:
- Lee la cámara con **PyAV** (v4l2) en un hilo propio y mantiene el ÚLTIMO
  frame como imagen PIL ya redimensionada al tamaño del preview. La GUI (hilo
  principal) lo consulta con `latest()` y lo pinta con ImageTk (crear ImageTk
  debe hacerse en el hilo de Tkinter).
- **Pausable** (`set_wanted`): solo decodifica cuando el preview está visible
  (pantalla principal). En otras pantallas —llamada WebRTC, asistente IA— se
  pausa para no competir por CPU/audio en la Raspberry Pi 3B.
- TOLERANTE A FALLOS: si faltan PyAV/Pillow o la cámara, queda inactivo
  (`disponible == False`) y el kiosco funciona igual, sin preview.
"""

from __future__ import annotations

import time
import logging
import threading

logger = logging.getLogger(__name__)

try:
    import av
    from PIL import Image  # noqa: F401  (Image se usa vía frame.to_image())
    _CAM_OK = True
except Exception as _e:  # noqa: BLE001
    _CAM_OK = False
    logger.info("Cámara no disponible (%s); el kiosco funcionará sin preview.", _e)


class CameraService:
    """Lee una webcam UVC y expone el último frame como imagen PIL."""

    def __init__(self, dispositivo: str = "/dev/video0",
                 ancho: int = 160, alto: int = 120, fps: int = 10):
        self.dispositivo = dispositivo
        self.ancho = ancho
        self.alto = alto
        self.fps = fps
        self.disponible = _CAM_OK

        self._thread: threading.Thread | None = None
        self._stop = False
        self._wanted = False           # solo decodifica cuando el preview se muestra
        self._latest = None            # última imagen PIL (ya redimensionada)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def iniciar(self):
        if not self.disponible or (self._thread and self._thread.is_alive()):
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def detener(self):
        self._stop = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def set_wanted(self, wanted: bool):
        """Activa/pausa la captura (True solo cuando el preview está visible)."""
        self._wanted = bool(wanted)

    def latest(self):
        """Última imagen PIL redimensionada (o None si aún no hay frame)."""
        with self._lock:
            return self._latest

    # ------------------------------------------------------------------ #
    def _run(self):
        while not self._stop:
            if not self._wanted:
                time.sleep(0.3)
                continue
            try:
                cont = av.open(self.dispositivo, format="v4l2",
                               options={"video_size": "640x480", "framerate": str(self.fps)})
                for frame in cont.decode(video=0):
                    if self._stop or not self._wanted:
                        break
                    try:
                        img = frame.to_image()  # PIL Image (RGB)
                        img = img.resize((self.ancho, self.alto), Image.BILINEAR)
                    except Exception:  # noqa: BLE001
                        continue
                    with self._lock:
                        self._latest = img
                try:
                    cont.close()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                logger.warning("Cámara: error (%s); reintento en 2s.", str(e)[:120])
                time.sleep(2)
        with self._lock:
            self._latest = None
