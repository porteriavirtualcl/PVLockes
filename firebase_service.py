"""
firebase_service.py
-------------------
Conexión y operaciones con Firebase Firestore, alineado al esquema de la app
de PRODUCCIÓN ("porteria-virtual"):

    users/{uid}
        { name, email, unit, condoId, role: 'resident', status, fcmToken }

    condos/{condoId}/parcels/{parcelId}
        { condoId, condoName, residentName, residentUserId, unit,
          status: 'pending'|'picked_up', arrivedAt (Timestamp), pickedUpAt,
          createdByName, courier, ... }

Este servicio NO es la fuente de verdad del kiosco: es el "espejo" remoto. La
fuente de verdad local es LocalStore (SQLite). El SyncService orquesta ambos.

Si Firebase no responde, se levanta FirebaseNoDisponibleError y el kiosco
sigue operando offline.
"""

from __future__ import annotations

import os
import logging
import datetime

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()


class FirebaseNoDisponibleError(Exception):
    """No se pudo contactar/inicializar Firebase (el kiosco sigue offline)."""


def _iso_a_datetime(valor: str | None):
    """Convierte un ISO-8601 a datetime timezone-aware (para Timestamp de Firestore)."""
    if not valor:
        return None
    try:
        return datetime.datetime.fromisoformat(valor)
    except (ValueError, TypeError):
        return None


class FirebaseService:

    def __init__(self, iniciar_conexion: bool = True):
        self.db = None
        self._conectado = False
        if iniciar_conexion:
            self.conectar()

    # ------------------------------------------------------------------ #
    # Conexión
    # ------------------------------------------------------------------ #
    def conectar(self):
        """Inicializa Firebase Admin con las credenciales del .env."""
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
            if not cred_path or not os.path.exists(cred_path):
                raise FirebaseNoDisponibleError(
                    f"No se encontró el archivo de credenciales: {cred_path}"
                )

            if not firebase_admin._apps:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)

            self.db = firestore.client()
            self._conectado = True
            logger.info("Conexión a Firebase establecida.")
        except FirebaseNoDisponibleError:
            raise
        except ImportError as e:
            raise FirebaseNoDisponibleError(f"Librería firebase-admin no instalada: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error("Fallo al conectar con Firebase: %s", e)
            raise FirebaseNoDisponibleError(str(e))

    @property
    def conectado(self) -> bool:
        return self._conectado

    def _asegurar_conexion(self):
        if not self._conectado or self.db is None:
            raise FirebaseNoDisponibleError("No hay conexión activa con Firebase.")

    # ------------------------------------------------------------------ #
    # Residentes: descarga desde 'users' (para cachear en LocalStore)
    # ------------------------------------------------------------------ #
    def descargar_residentes(self, condo_id: str) -> list:
        """
        Devuelve los residentes del condominio desde la colección 'users'.

        Cada item: {uid, nombre, email, unit, status, fcm_token}

        Raises:
            FirebaseNoDisponibleError: si Firebase no responde.
        """
        self._asegurar_conexion()
        try:
            from firebase_admin import firestore
            consulta = (
                self.db.collection("users")
                .where(filter=firestore.FieldFilter("role", "==", "resident"))
                .where(filter=firestore.FieldFilter("condoId", "==", condo_id))
            )
            docs = list(consulta.stream())
        except Exception as e:  # noqa: BLE001
            logger.error("Error descargando residentes del condo %s: %s", condo_id, e)
            raise FirebaseNoDisponibleError(str(e))

        residentes = []
        for doc in docs:
            d = doc.to_dict()
            residentes.append({
                "uid": doc.id,
                "nombre": d.get("name", "Sin nombre"),
                "email": d.get("email", ""),
                "unit": str(d.get("unit", "")),
                "status": d.get("status", "Activo"),
                "fcm_token": d.get("fcmToken", ""),
            })
        logger.info("Descargados %s residentes del condo %s.", len(residentes), condo_id)
        return residentes

    def descargar_kiosk(self, kiosk_id: str) -> dict | None:
        """
        Descarga la configuración LÓGICA de este equipo desde 'kiosks/{kiosk_id}'.

        Returns:
            dict con la config remota (tipo, operacionPuertas, tipoUnidad,
            orientacion, retiroAutomatico, lockers[], buzon, activo, ...),
            o None si el documento no existe.

        Raises:
            FirebaseNoDisponibleError: si Firebase no responde.
        """
        if not kiosk_id:
            return None
        self._asegurar_conexion()
        try:
            doc = self.db.collection("kiosks").document(kiosk_id).get()
        except Exception as e:  # noqa: BLE001
            logger.error("Error descargando config del kiosco %s: %s", kiosk_id, e)
            raise FirebaseNoDisponibleError(str(e))

        if not doc.exists:
            logger.info("Kiosco %s no tiene config en Firestore (se usa fallback local).", kiosk_id)
            return None
        return doc.to_dict()

    def descargar_couriers(self) -> list:
        """
        Descarga la lista de couriers desde 'config/courierCompanies'
        (campo 'companies'), la misma que administra la app de producción.

        Returns:
            Lista de nombres (str). Vacía si el documento no existe.

        Raises:
            FirebaseNoDisponibleError: si Firebase no responde.
        """
        self._asegurar_conexion()
        try:
            doc = self.db.collection("config").document("courierCompanies").get()
        except Exception as e:  # noqa: BLE001
            logger.error("Error descargando couriers: %s", e)
            raise FirebaseNoDisponibleError(str(e))

        if not doc.exists:
            return []
        data = doc.to_dict() or {}
        companies = data.get("companies", [])
        return [str(c) for c in companies if c]

    # ------------------------------------------------------------------ #
    # Encomiendas: crear/actualizar en 'condos/{condoId}/parcels'
    # ------------------------------------------------------------------ #
    def crear_parcel(self, condo_id: str, parcel_id: str, datos: dict, kiosk_id: str = ""):
        """
        Escribe la encomienda con un doc-id FIJO (el que generó el kiosco = QR).
        Usa .set() para respetar ese id, de modo que el QR coincida con el doc.

        `datos` (registro local de LocalStore) debe traer al menos:
            condo_name, unit, resident_name, resident_user_id, status,
            arrived_at (ISO), picked_up_at (ISO|None), created_by_name,
            courier, locker_id, tipo_recurso, tamano

        Raises:
            FirebaseNoDisponibleError: si Firebase no responde.
        """
        self._asegurar_conexion()

        # Campos del esquema de producción + extras aditivos (source, lockerId...).
        payload = {
            "condoId": condo_id,
            "condoName": datos.get("condo_name", ""),
            "residentName": datos.get("resident_name", ""),
            "residentUserId": datos.get("resident_user_id", ""),
            "unit": str(datos.get("unit", "")),
            "status": datos.get("status", "pending"),
            "arrivedAt": _iso_a_datetime(datos.get("arrived_at")),
            "pickedUpAt": _iso_a_datetime(datos.get("picked_up_at")),
            "createdByName": datos.get("created_by_name", "Kiosco"),
            "courier": datos.get("courier", ""),
            # Campos adicionales propios del kiosco (la app los ignora si no los usa):
            "lockerId": datos.get("locker_id", ""),
            "tipoRecurso": datos.get("tipo_recurso", ""),
            "tamano": datos.get("tamano", ""),
            "kioskId": kiosk_id,
            "source": "kiosk",
        }
        try:
            (self.db.collection("condos").document(condo_id)
                 .collection("parcels").document(parcel_id).set(payload))
            logger.info("Parcel %s creado en Firebase (condo %s).", parcel_id, condo_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Error creando parcel %s: %s", parcel_id, e)
            raise FirebaseNoDisponibleError(str(e))

    def actualizar_parcel(self, condo_id: str, parcel_id: str, campos: dict):
        """
        Actualiza campos de una encomienda existente (ej. marcar 'picked_up').
        `campos` usa llaves del esquema de producción (status, pickedUpAt...).

        Raises:
            FirebaseNoDisponibleError: si Firebase no responde.
        """
        self._asegurar_conexion()
        try:
            (self.db.collection("condos").document(condo_id)
                 .collection("parcels").document(parcel_id).update(campos))
            logger.info("Parcel %s actualizado en Firebase.", parcel_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Error actualizando parcel %s: %s", parcel_id, e)
            raise FirebaseNoDisponibleError(str(e))


# ---------------------------------------------------------------------------
# Prueba rápida (requiere .env + credenciales; si no, muestra el error esperado).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        fb = FirebaseService()
        print("Conectado:", fb.conectado)
    except FirebaseNoDisponibleError as e:
        print(f"[Servicio en mantención] {e}")
