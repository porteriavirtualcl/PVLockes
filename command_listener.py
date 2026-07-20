"""
command_listener.py
--------------------
Escucha comandos de apertura remota (operador desde la app) en
`kiosks/{kiosk_id}/commands`, por POLLING sobre la API REST de Firestore.

(Antes usaba on_snapshot de gRPC, pero gRPC se cuelga en la Raspberry Pi 3B;
por eso se hace polling REST cada N segundos, tolerante a cortes de red.)

Cada comando:
    { accion:"abrir", lockerId:"L3", operacion:"retiro"|"deposito",
      estado:"pendiente"|"ejecutado"|"error", ... }

El kiosco ejecuta la apertura con su PIN LOCAL (los pines nunca viajan por red)
y marca el estado del comando.
"""

from __future__ import annotations

import logging
import datetime
import threading

from firebase_service import FirebaseNoDisponibleError

logger = logging.getLogger(__name__)


class CommandListener:

    def __init__(self, firebase, kiosk_id: str, on_abrir, intervalo_seg: int = 8):
        """
        Args:
            firebase: instancia de FirebaseService.
            kiosk_id: id de este equipo.
            on_abrir: callback (locker_id, operacion) -> (ok: bool, error: str).
            intervalo_seg: cada cuánto revisa comandos pendientes.
        """
        self.firebase = firebase
        self.kiosk_id = kiosk_id
        self.on_abrir = on_abrir
        self.intervalo = intervalo_seg
        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def iniciar(self) -> bool:
        if not self.kiosk_id or self.firebase is None or not self.firebase.conectado:
            logger.info("Apertura remota inactiva (sin conexión o sin kiosk_id).")
            return False
        self._stop.clear()
        self._hilo = threading.Thread(target=self._loop, name="CommandListener", daemon=True)
        self._hilo.start()
        logger.info("Apertura remota ACTIVA (polling cada %ss) para %s.",
                    self.intervalo, self.kiosk_id)
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                for c in self.firebase.obtener_comandos_pendientes(self.kiosk_id):
                    self._procesar(c)
            except FirebaseNoDisponibleError:
                pass  # sin conexión; se reintenta en el próximo ciclo
            except Exception as e:  # noqa: BLE001 - el hilo nunca debe morir
                logger.error("Error en polling de comandos: %s", e)
            self._stop.wait(self.intervalo)

    def _procesar(self, c: dict):
        cmd_id = c.get("id")
        locker_id = c.get("lockerId")
        operacion = c.get("operacion", "retiro")
        logger.info("Comando de apertura remota: locker=%s operacion=%s", locker_id, operacion)

        ok, error = False, ""
        try:
            ok, error = self.on_abrir(locker_id, operacion)
        except Exception as e:  # noqa: BLE001
            ok, error = False, str(e)

        try:
            ahora = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.firebase.actualizar_comando(
                self.kiosk_id, cmd_id, "ejecutado" if ok else "error", error, ahora)
        except Exception as e:  # noqa: BLE001
            logger.error("No se pudo actualizar el comando %s: %s", cmd_id, e)

    # ------------------------------------------------------------------ #
    def detener(self):
        self._stop.set()
        if self._hilo:
            self._hilo.join(timeout=2)
        logger.info("Listener de comandos detenido.")
