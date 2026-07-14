"""
command_listener.py
--------------------
Escucha en TIEMPO REAL comandos de apertura remota enviados por un operador
desde la app de producción, en `kiosks/{kiosk_id}/commands`.

Cada comando:
    { accion: "abrir", lockerId: "L3", operacion: "retiro"|"deposito",
      estado: "pendiente"|"ejecutado"|"error", ... }

El kiosco (Admin SDK) mantiene un listener de Firestore; al llegar un comando
pendiente, ejecuta la apertura con su PIN LOCAL y actualiza el estado. Los pines
nunca viajan por la red: el operador solo indica el id del locker.

Si no hay conexión/credenciales (ej. desarrollo), queda inactivo sin romper nada.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class CommandListener:

    def __init__(self, firebase, kiosk_id: str, on_abrir):
        """
        Args:
            firebase: instancia de FirebaseService (con .db y .conectado).
            kiosk_id: id de este equipo.
            on_abrir: callback (locker_id, operacion) -> (ok: bool, error: str).
        """
        self.firebase = firebase
        self.kiosk_id = kiosk_id
        self.on_abrir = on_abrir
        self._watch = None

    # ------------------------------------------------------------------ #
    def iniciar(self) -> bool:
        if not self.kiosk_id or self.firebase is None or not self.firebase.conectado:
            logger.info("Apertura remota inactiva (sin conexión o sin kiosk_id).")
            return False
        try:
            from firebase_admin import firestore
            col = (
                self.firebase.db.collection("kiosks").document(self.kiosk_id)
                .collection("commands")
                .where(filter=firestore.FieldFilter("estado", "==", "pendiente"))
            )
            self._watch = col.on_snapshot(self._on_snapshot)
            logger.info("Apertura remota ACTIVA (escuchando comandos de %s).", self.kiosk_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("No se pudo iniciar el listener de comandos: %s", e)
            return False

    # ------------------------------------------------------------------ #
    def _on_snapshot(self, col_snapshot, changes, read_time):
        # Corre en un hilo del SDK de Firestore.
        for change in changes:
            if change.type.name != "ADDED":
                continue
            self._procesar(change.document)

    def _procesar(self, doc):
        data = doc.to_dict() or {}
        if data.get("estado") != "pendiente":
            return

        locker_id = data.get("lockerId")
        operacion = data.get("operacion", "retiro")
        logger.info("Comando de apertura remota: locker=%s operacion=%s", locker_id, operacion)

        ok, error = False, ""
        try:
            ok, error = self.on_abrir(locker_id, operacion)
        except Exception as e:  # noqa: BLE001
            ok, error = False, str(e)

        try:
            from firebase_admin import firestore
            doc.reference.update({
                "estado": "ejecutado" if ok else "error",
                "error": error or "",
                "executedAt": firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:  # noqa: BLE001
            logger.error("No se pudo actualizar el comando %s: %s", doc.id, e)

    # ------------------------------------------------------------------ #
    def detener(self):
        if self._watch is not None:
            try:
                self._watch.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        logger.info("Listener de comandos detenido.")
