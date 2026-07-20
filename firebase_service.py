"""
firebase_service.py
-------------------
Acceso a Firestore vía API REST (HTTPS), NO gRPC.

Motivo: en la Raspberry Pi 3B el cliente gRPC de firebase-admin/google-cloud
se cuelga (problema de transporte gRPC en armv7). La API REST sobre HTTPS
funciona sin problemas. Se autentica con la service account usando google-auth
(que obtiene el token OAuth2 por HTTP).

Esquema de producción:
    users/{uid}                         -> residentes (role='resident')
    condos/{condoId}/parcels/{parcelId} -> encomiendas
    config/courierCompanies             -> { companies: [...] }
    kiosks/{kioskId}                     -> config lógica del equipo
    kiosks/{kioskId}/commands/{cmdId}    -> comandos de apertura remota

Si no hay red/credenciales, se levanta FirebaseNoDisponibleError y el kiosco
sigue operando offline.
"""

from __future__ import annotations

import os
import logging

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

_TIMEOUT = 20          # segundos por request (evita cuelgues)
_SCOPE = "https://www.googleapis.com/auth/datastore"


class FirebaseNoDisponibleError(Exception):
    """No se pudo contactar/inicializar Firebase (el kiosco sigue offline)."""


# --------------------------------------------------------------------------- #
# Conversión de valores Firestore REST <-> Python
# --------------------------------------------------------------------------- #
def _to_value(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _to_value(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_to_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _from_value(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "nullValue" in v:
        return None
    if "mapValue" in v:
        return {k: _from_value(x) for k, x in v.get("mapValue", {}).get("fields", {}).items()}
    if "arrayValue" in v:
        return [_from_value(x) for x in v.get("arrayValue", {}).get("values", [])]
    if "referenceValue" in v:
        return v["referenceValue"]
    return None


def _fields_to_dict(fields: dict) -> dict:
    return {k: _from_value(v) for k, v in (fields or {}).items()}


def _dict_to_fields(d: dict) -> dict:
    return {k: _to_value(v) for k, v in d.items()}


def _iso_to_rfc3339(iso):
    """Convierte un ISO-8601 local a timestamp RFC3339 (con 'Z') para Firestore."""
    if not iso:
        return None
    s = str(iso)
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    if "Z" in s or "+" in s:
        return s
    return s + "Z"


class FirebaseService:

    def __init__(self, iniciar_conexion: bool = True):
        self._session = None
        self._base = ""
        self.project_id = ""
        self._conectado = False
        if iniciar_conexion:
            self.conectar()

    # ------------------------------------------------------------------ #
    def conectar(self):
        """Carga la service account y crea la sesión REST autenticada."""
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession

            cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
            if not cred_path or not os.path.exists(cred_path):
                raise FirebaseNoDisponibleError(
                    f"No se encontró el archivo de credenciales: {cred_path}"
                )
            creds = service_account.Credentials.from_service_account_file(
                cred_path, scopes=[_SCOPE]
            )
            self.project_id = creds.project_id or os.getenv("FIREBASE_PROJECT_ID", "")
            self._session = AuthorizedSession(creds)
            self._base = (
                f"https://firestore.googleapis.com/v1/projects/"
                f"{self.project_id}/databases/(default)/documents"
            )
            self._conectado = True
            logger.info("Firebase (REST) inicializado para el proyecto %s.", self.project_id)
        except FirebaseNoDisponibleError:
            raise
        except ImportError as e:
            raise FirebaseNoDisponibleError(f"Falta google-auth/requests: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error("Fallo al inicializar Firebase REST: %s", e)
            raise FirebaseNoDisponibleError(str(e))

    @property
    def conectado(self) -> bool:
        return self._conectado

    def _asegurar_conexion(self):
        if not self._conectado or self._session is None:
            raise FirebaseNoDisponibleError("No hay sesión activa con Firebase.")

    def _get(self, path: str):
        self._asegurar_conexion()
        try:
            return self._session.get(f"{self._base}/{path}", timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise FirebaseNoDisponibleError(str(e))

    def _post(self, path: str, body: dict):
        self._asegurar_conexion()
        try:
            return self._session.post(f"{self._base}/{path}", json=body, timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise FirebaseNoDisponibleError(str(e))

    def _patch(self, path: str, body: dict):
        self._asegurar_conexion()
        try:
            return self._session.patch(f"{self._base}/{path}", json=body, timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise FirebaseNoDisponibleError(str(e))

    def _runquery(self, parent: str, structured: dict) -> list:
        """Ejecuta un runQuery. `parent` es '' (raíz) o 'kiosks/{id}' etc."""
        path = (f"{parent}:runQuery" if parent else ":runQuery")
        r = self._post(path, {"structuredQuery": structured})
        if r.status_code != 200:
            raise FirebaseNoDisponibleError(f"runQuery {r.status_code}: {r.text[:200]}")
        docs = []
        for row in r.json():
            if "document" in row:
                docs.append(row["document"])
        return docs

    @staticmethod
    def _doc_id(doc: dict) -> str:
        return doc.get("name", "").split("/")[-1]

    # ------------------------------------------------------------------ #
    # Residentes: 'users' where role=='resident' and condoId==...
    # ------------------------------------------------------------------ #
    def descargar_residentes(self, condo_id: str) -> list:
        self._asegurar_conexion()
        query = {
            "from": [{"collectionId": "users"}],
            "where": {"compositeFilter": {"op": "AND", "filters": [
                {"fieldFilter": {"field": {"fieldPath": "role"}, "op": "EQUAL",
                                 "value": {"stringValue": "resident"}}},
                {"fieldFilter": {"field": {"fieldPath": "condoId"}, "op": "EQUAL",
                                 "value": {"stringValue": condo_id}}},
            ]}},
        }
        docs = self._runquery("", query)
        residentes = []
        for doc in docs:
            d = _fields_to_dict(doc.get("fields", {}))
            residentes.append({
                "uid": self._doc_id(doc),
                "nombre": d.get("name", "Sin nombre"),
                "email": d.get("email", ""),
                "unit": str(d.get("unit", "")),
                "status": d.get("status", "Activo"),
                "fcm_token": d.get("fcmToken", ""),
            })
        logger.info("Descargados %s residentes del condo %s.", len(residentes), condo_id)
        return residentes

    # ------------------------------------------------------------------ #
    # Couriers: config/courierCompanies.companies
    # ------------------------------------------------------------------ #
    def descargar_couriers(self) -> list:
        r = self._get("config/courierCompanies")
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise FirebaseNoDisponibleError(f"couriers {r.status_code}: {r.text[:200]}")
        d = _fields_to_dict(r.json().get("fields", {}))
        companies = d.get("companies", [])
        return [str(c) for c in companies if c]

    # ------------------------------------------------------------------ #
    # Config del kiosco: kiosks/{kioskId}
    # ------------------------------------------------------------------ #
    def descargar_kiosk(self, kiosk_id: str) -> dict | None:
        if not kiosk_id:
            return None
        r = self._get(f"kiosks/{kiosk_id}")
        if r.status_code == 404:
            logger.info("Kiosco %s sin config en Firestore (fallback local).", kiosk_id)
            return None
        if r.status_code != 200:
            raise FirebaseNoDisponibleError(f"kiosk {r.status_code}: {r.text[:200]}")
        return _fields_to_dict(r.json().get("fields", {}))

    # ------------------------------------------------------------------ #
    # Encomiendas: crear/actualizar en condos/{condoId}/parcels
    # ------------------------------------------------------------------ #
    def crear_parcel(self, condo_id: str, parcel_id: str, datos: dict, kiosk_id: str = ""):
        fields = _dict_to_fields({
            "condoId": condo_id,
            "condoName": datos.get("condo_name", ""),
            "residentName": datos.get("resident_name", ""),
            "residentUserId": datos.get("resident_user_id", ""),
            "unit": str(datos.get("unit", "")),
            "status": datos.get("status", "pending"),
            "createdByName": datos.get("created_by_name", "Kiosco"),
            "courier": datos.get("courier", ""),
            "lockerId": datos.get("locker_id", ""),
            "tipoRecurso": datos.get("tipo_recurso", ""),
            "tamano": datos.get("tamano", ""),
            "kioskId": kiosk_id,
            "source": "kiosk",
        })
        fields["arrivedAt"] = {"timestampValue": _iso_to_rfc3339(datos.get("arrived_at"))}
        pu = datos.get("picked_up_at")
        fields["pickedUpAt"] = ({"timestampValue": _iso_to_rfc3339(pu)} if pu
                                else {"nullValue": None})

        r = self._patch(f"condos/{condo_id}/parcels/{parcel_id}", {"fields": fields})
        if r.status_code not in (200, 201):
            raise FirebaseNoDisponibleError(f"crear_parcel {r.status_code}: {r.text[:200]}")
        logger.info("Parcel %s creado en Firebase (condo %s).", parcel_id, condo_id)

    def actualizar_parcel(self, condo_id: str, parcel_id: str, campos: dict):
        """campos: {status, picked_up_at(ISO|None)}."""
        fields = {"status": _to_value(campos.get("status", "picked_up"))}
        pu = campos.get("picked_up_at")
        fields["pickedUpAt"] = ({"timestampValue": _iso_to_rfc3339(pu)} if pu
                                else {"nullValue": None})
        path = (f"condos/{condo_id}/parcels/{parcel_id}"
                "?updateMask.fieldPaths=status&updateMask.fieldPaths=pickedUpAt")
        r = self._patch(path, {"fields": fields})
        if r.status_code not in (200, 201):
            raise FirebaseNoDisponibleError(f"actualizar_parcel {r.status_code}: {r.text[:200]}")
        logger.info("Parcel %s actualizado en Firebase.", parcel_id)

    # ------------------------------------------------------------------ #
    # Comandos de apertura remota (polling; REST no tiene listener realtime)
    # ------------------------------------------------------------------ #
    def obtener_comandos_pendientes(self, kiosk_id: str) -> list:
        if not kiosk_id:
            return []
        query = {
            "from": [{"collectionId": "commands"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "estado"}, "op": "EQUAL",
                                      "value": {"stringValue": "pendiente"}}},
        }
        docs = self._runquery(f"kiosks/{kiosk_id}", query)
        out = []
        for doc in docs:
            d = _fields_to_dict(doc.get("fields", {}))
            d["id"] = self._doc_id(doc)
            out.append(d)
        return out

    def actualizar_comando(self, kiosk_id: str, cmd_id: str, estado: str,
                           error: str = "", executed_at_iso: str | None = None):
        fields = {"estado": _to_value(estado), "error": _to_value(error or "")}
        fields["executedAt"] = ({"timestampValue": _iso_to_rfc3339(executed_at_iso)}
                                if executed_at_iso else {"nullValue": None})
        path = (f"kiosks/{kiosk_id}/commands/{cmd_id}"
                "?updateMask.fieldPaths=estado&updateMask.fieldPaths=error"
                "&updateMask.fieldPaths=executedAt")
        r = self._patch(path, {"fields": fields})
        if r.status_code not in (200, 201):
            raise FirebaseNoDisponibleError(f"actualizar_comando {r.status_code}: {r.text[:200]}")
