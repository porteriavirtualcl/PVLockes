"""
sync_service.py
---------------
Orquesta la sincronización entre el almacenamiento local (LocalStore/SQLite) y
Firebase (FirebaseService), en un hilo de fondo.

Dos direcciones:
  - Residentes:  Firebase 'users'  ->  caché local  (para operar offline).
  - Encomiendas: local  ->  Firebase 'condos/{condoId}/parcels'.

Tolerante a fallos: si no hay internet, cada ciclo simplemente no logra
contactar Firebase, lo registra y reintenta en el siguiente ciclo. El kiosco
nunca se bloquea por falta de red.
"""

from __future__ import annotations

import logging
import threading

from firebase_service import FirebaseService, FirebaseNoDisponibleError, _iso_a_datetime

logger = logging.getLogger(__name__)


class SyncService:

    def __init__(self, local_store, firebase: FirebaseService | None,
                 condo_id: str, intervalo_seg: int = 60, max_reintentos: int = 5,
                 kiosk_id: str = ""):
        self.local = local_store
        self.firebase = firebase
        self.condo_id = condo_id
        self.kiosk_id = kiosk_id
        self.intervalo = intervalo_seg
        self.max_reintentos = max_reintentos

        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Conexión (reintenta reconectar si estaba caída)
    # ------------------------------------------------------------------ #
    def _asegurar_firebase(self) -> bool:
        """Devuelve True si hay conexión utilizable con Firebase."""
        if self.firebase is None:
            try:
                self.firebase = FirebaseService()
            except FirebaseNoDisponibleError as e:
                logger.debug("Firebase sigue no disponible: %s", e)
                return False
            return self.firebase.conectado

        if not self.firebase.conectado:
            try:
                self.firebase.conectar()
            except FirebaseNoDisponibleError as e:
                logger.debug("No se pudo reconectar a Firebase: %s", e)
                return False
        return self.firebase.conectado

    # ------------------------------------------------------------------ #
    # Un ciclo de sincronización
    # ------------------------------------------------------------------ #
    def ciclo(self) -> bool:
        """
        Ejecuta un ciclo: refresca residentes y empuja encomiendas pendientes.
        Devuelve True si se pudo contactar Firebase, False si está offline.
        """
        if not self._asegurar_firebase():
            logger.info("Sync: sin conexión con Firebase (se reintentará).")
            return False

        self._sincronizar_kiosk()
        self._sincronizar_residentes()
        self._sincronizar_couriers()
        self._empujar_pendientes()
        return True

    def _sincronizar_kiosk(self):
        """Descarga la config lógica del equipo y la cachea (se aplica al reiniciar)."""
        if not self.kiosk_id:
            return
        try:
            remoto = self.firebase.descargar_kiosk(self.kiosk_id)
            if remoto:
                self.local.guardar_kiosk_config(self.kiosk_id, remoto)
        except FirebaseNoDisponibleError as e:
            logger.warning("No se pudo descargar la config del kiosco: %s", e)

    def _sincronizar_residentes(self):
        try:
            residentes = self.firebase.descargar_residentes(self.condo_id)
            self.local.upsert_residentes(residentes)
        except FirebaseNoDisponibleError as e:
            logger.warning("No se pudieron descargar residentes: %s", e)

    def _sincronizar_couriers(self):
        try:
            couriers = self.firebase.descargar_couriers()
            if couriers:
                self.local.guardar_couriers(couriers)
        except FirebaseNoDisponibleError as e:
            logger.warning("No se pudieron descargar couriers: %s", e)

    def _empujar_pendientes(self):
        pendientes = self.local.get_pendientes_sync()
        if not pendientes:
            return
        logger.info("Sync: %s encomienda(s) pendiente(s) de subir.", len(pendientes))

        for enc in pendientes:
            pid = enc["parcel_id"]
            try:
                if not enc.get("remote_creado"):
                    # Crear el documento con el mismo id (= QR).
                    self.firebase.crear_parcel(self.condo_id, pid, enc, kiosk_id=self.kiosk_id)
                    self.local.marcar_sincronizada(pid, remote_creado=True)
                else:
                    # Ya existe: es una actualización (ej. retiro).
                    campos = {
                        "status": enc.get("status", "pending"),
                        "pickedUpAt": _iso_a_datetime(enc.get("picked_up_at")),
                    }
                    self.firebase.actualizar_parcel(self.condo_id, pid, campos)
                    self.local.marcar_sincronizada(pid, remote_creado=True)
            except FirebaseNoDisponibleError as e:
                # Si se cayó la conexión a mitad de ciclo, no seguir intentando.
                self.local.registrar_error_sync(pid, str(e), self.max_reintentos)
                logger.warning("Sync interrumpido (se reintentará): %s", e)
                break

    # ------------------------------------------------------------------ #
    # Hilo de fondo
    # ------------------------------------------------------------------ #
    def iniciar(self):
        """Arranca el hilo periódico de sincronización (daemon)."""
        if self._hilo and self._hilo.is_alive():
            return
        self._stop.clear()
        self._hilo = threading.Thread(target=self._loop, name="SyncService", daemon=True)
        self._hilo.start()
        logger.info("SyncService iniciado (intervalo=%ss).", self.intervalo)

    def _loop(self):
        # Primer ciclo inmediato al arrancar.
        while not self._stop.is_set():
            try:
                self.ciclo()
            except Exception as e:  # noqa: BLE001 - el hilo nunca debe morir
                logger.error("Error inesperado en ciclo de sync: %s", e)
            # Espera interrumpible: si detienen el servicio, sale de inmediato.
            self._stop.wait(self.intervalo)

    def sincronizar_ahora(self):
        """Dispara un ciclo puntual en un hilo aparte (ej. tras dejar una encomienda)."""
        threading.Thread(target=self.ciclo, name="SyncNow", daemon=True).start()

    def detener(self):
        self._stop.set()
        if self._hilo:
            self._hilo.join(timeout=2)
        logger.info("SyncService detenido.")
