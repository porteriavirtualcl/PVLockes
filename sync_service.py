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

from firebase_service import FirebaseService, FirebaseNoDisponibleError

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
        # Un solo ciclo a la vez. `sincronizar_ahora()` lanza un hilo propio y
        # puede caer encima del hilo periódico: dos ciclos en paralelo suben la
        # misma encomienda dos veces (y avisan al residente dos veces), y se
        # pelean el lock de SQLite con el hilo del lector de QR.
        self._ciclo_en_curso = threading.Lock()

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
        Devuelve True si se pudo contactar Firebase, False si está offline o si
        ya había otro ciclo en curso.

        Los ciclos NO se solapan: si el periódico y uno puntual coinciden, el
        segundo se descarta. No se pierde nada, porque lo pendiente sigue
        pendiente para el ciclo que ya está corriendo o para el siguiente.
        """
        if not self._ciclo_en_curso.acquire(blocking=False):
            logger.info("Sync: ya hay un ciclo en curso; se omite este.")
            return False
        try:
            if not self._asegurar_firebase():
                logger.info("Sync: sin conexión con Firebase (se reintentará).")
                return False

            self._sincronizar_kiosk()
            self._sincronizar_residentes()
            self._sincronizar_couriers()
            self._empujar_pendientes()
            self._liberar_retirados_en_app()
            return True
        finally:
            self._ciclo_en_curso.release()

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
                    # Todos los residentes de la unidad (para que el QR llegue a
                    # cualquiera del depto con app, no solo al destinatario).
                    enc["unit_user_ids"] = self._uids_unidad(enc.get("unit", ""))
                    # Crear el documento con el mismo id (= QR).
                    self.firebase.crear_parcel(self.condo_id, pid, enc, kiosk_id=self.kiosk_id)
                    self.local.marcar_sincronizada(pid, remote_creado=True)
                    # Recién ahora el doc existe en Firestore y la app puede
                    # dibujar el QR: es el momento de avisarle al residente.
                    # Va aquí y no en el depósito para que también funcione
                    # cuando el kiosco estaba sin internet.
                    self._avisar_encomienda(enc)
                else:
                    # Ya existe: es una actualización (ej. retiro).
                    self.firebase.actualizar_parcel(self.condo_id, pid, {
                        "status": enc.get("status", "pending"),
                        "picked_up_at": enc.get("picked_up_at"),
                    })
                    self.local.marcar_sincronizada(pid, remote_creado=True)
            except FirebaseNoDisponibleError as e:
                # Si se cayó la conexión a mitad de ciclo, no seguir intentando.
                self.local.registrar_error_sync(pid, str(e), self.max_reintentos)
                logger.warning("Sync interrumpido (se reintentará): %s", e)
                break

    def _liberar_retirados_en_app(self):
        """
        Libera los casilleros de encomiendas que fueron marcadas como retiradas
        DESDE la app o el operador (no por el lector del kiosco).

        El kiosco es offline-first y solo EMPUJA a Firestore; sin este paso, una
        encomienda marcada 'picked_up' en la app quedaría 'pending' en la base
        local para siempre y el casillero nunca se liberaría.

        Se consulta solo por las que hoy ocupan un casillero (pocas), así que el
        costo es de unos pocos GET por ciclo.
        """
        try:
            ocupando = self.local.encomiendas_ocupando()
        except Exception as e:  # noqa: BLE001
            logger.warning("No se pudo listar encomiendas ocupando: %s", e)
            return

        for enc in ocupando:
            pid = enc["parcel_id"]
            try:
                p = self.firebase.obtener_parcel(self.condo_id, pid)
            except FirebaseNoDisponibleError:
                return  # se cayó la conexión; se reintenta en el próximo ciclo
            except Exception as e:  # noqa: BLE001
                logger.warning("No se pudo consultar la encomienda %s: %s", pid, e)
                continue
            if p and p.get("status") == "picked_up":
                self.local.marcar_retirada_desde_remoto(pid, p.get("pickedUpAt"))

    def _uids_unidad(self, unit) -> list:
        """UIDs de todos los residentes de una unidad (para unitUserIds)."""
        try:
            return [r["uid"] for r in self.local.get_residentes_por_unidad(unit) if r.get("uid")]
        except Exception as e:  # noqa: BLE001
            logger.warning("No se pudo listar residentes de la unidad %s: %s", unit, e)
            return []

    def _avisar_encomienda(self, enc: dict):
        """
        Avisa por push que llegó una encomienda, a TODOS los residentes de la
        unidad que tengan la app (no solo al destinatario): en un depto puede
        haber varios y el asignado quizá no tiene la app.

        El QR de retiro es el `parcel_id`: la app lo dibuja a partir del dato
        `parcelId` que viaja en el mensaje. No se manda la imagen.

        Tolerante a fallos: si nadie tiene token o FCM rechaza, se registra y la
        sincronización sigue. La encomienda ya está depositada.
        """
        try:
            # Una encomienda depositada sin internet y retirada antes de que
            # el kiosco reconectara llega acá ya cerrada: avisar de su llegada
            # a esa altura sería un mensaje falso.
            if enc.get("status") != "pending":
                logger.info("Encomienda %s ya no está pendiente; no se avisa.",
                            enc.get("parcel_id", ""))
                return

            # Tokens de todos los residentes de la unidad, sin repetir.
            tokens = []
            for r in self.local.get_residentes_por_unidad(enc.get("unit", "")):
                t = r.get("fcm_token")
                if t and t not in tokens:
                    tokens.append(t)
            if not tokens:
                logger.info(
                    "Encomienda %s sin push: nadie de la unidad %s tiene la app activada.",
                    enc.get("parcel_id", ""), enc.get("unit", ""),
                )
                return

            locker = enc.get("locker_id", "")
            payload = dict(
                titulo="Llegó una encomienda",
                cuerpo=(f"Casillero {locker}. Toca para ver el código QR de retiro."
                        if locker else "Toca para ver los detalles."),
                datos={
                    "tipo": "encomienda_recibida",
                    "parcelId": enc.get("parcel_id", ""),
                    "lockerId": locker,
                    "condoId": self.condo_id,
                    "courier": enc.get("courier", ""),
                },
            )
            enviados = sum(1 for t in tokens if self.firebase.enviar_push(t, **payload))
            logger.info("Encomienda %s: push enviado a %s de %s residente(s) con app.",
                        enc.get("parcel_id", ""), enviados, len(tokens))
        except Exception as e:  # noqa: BLE001
            logger.warning("No se pudo avisar la encomienda %s: %s",
                           enc.get("parcel_id", ""), e)

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
