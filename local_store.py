"""
local_store.py
--------------
Almacenamiento LOCAL (SQLite) del kiosco "Portería Virtual".

Estrategia offline-first:
- Los datos viven primero en SQLite. El kiosco funciona SIN internet.
- Los residentes se cachean desde Firebase (colección 'users' de producción).
- Las encomiendas se crean localmente y luego el SyncService las empuja a
  Firebase ('condos/{condoId}/parcels') cuando hay conexión.

Detalle clave: el ID de cada encomienda (`parcel_id`) lo genera el propio
kiosco (UUID). Ese ID es a la vez:
  - el valor del código QR que verá el residente,
  - el futuro doc-id en Firestore.
Así, aunque no haya internet al dejar la encomienda, el QR ya es válido y al
sincronizar se escribe el documento con ese mismo ID.

SQLite se abre con check_same_thread=False + un Lock, porque acceden tanto el
hilo de la GUI como el hilo del SyncService.
"""

from __future__ import annotations

import json
import uuid
import sqlite3
import logging
import datetime
import threading

logger = logging.getLogger(__name__)


def _ahora_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class LocalStore:

    def __init__(self, ruta_db: str = "porteria_local.db"):
        self.ruta_db = ruta_db
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(ruta_db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._crear_tablas()
        logger.info("LocalStore inicializado en %s", ruta_db)

    # ------------------------------------------------------------------ #
    # Esquema
    # ------------------------------------------------------------------ #
    def _crear_tablas(self):
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS residentes (
                    uid        TEXT PRIMARY KEY,
                    nombre     TEXT,
                    email      TEXT,
                    unit       TEXT,
                    status     TEXT,
                    fcm_token  TEXT,
                    synced_at  TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS encomiendas (
                    parcel_id        TEXT PRIMARY KEY,
                    condo_id         TEXT,
                    condo_name       TEXT,
                    unit             TEXT,
                    resident_name    TEXT,
                    resident_user_id TEXT,
                    tamano           TEXT,
                    locker_id        TEXT,
                    tipo_recurso     TEXT,
                    courier          TEXT,
                    status           TEXT DEFAULT 'pending',
                    arrived_at       TEXT,
                    picked_up_at     TEXT,
                    created_by_name  TEXT,
                    remote_creado    INTEGER DEFAULT 0,
                    sync_status      TEXT DEFAULT 'pendiente',
                    sync_intentos    INTEGER DEFAULT 0,
                    last_error       TEXT,
                    created_at       TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS couriers (
                    nombre    TEXT PRIMARY KEY,
                    orden     INTEGER,
                    synced_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kiosk_config (
                    kiosk_id  TEXT PRIMARY KEY,
                    data      TEXT,
                    synced_at TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_res_unit ON residentes(unit)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enc_sync ON encomiendas(sync_status)"
            )

    # ------------------------------------------------------------------ #
    # Residentes (caché desde Firebase 'users')
    # ------------------------------------------------------------------ #
    def upsert_residentes(self, residentes: list):
        """Reemplaza/actualiza la caché local de residentes desde Firebase."""
        ahora = _ahora_iso()
        with self._lock, self._conn:
            for r in residentes:
                self._conn.execute(
                    """
                    INSERT INTO residentes (uid, nombre, email, unit, status, fcm_token, synced_at)
                    VALUES (:uid, :nombre, :email, :unit, :status, :fcm_token, :synced_at)
                    ON CONFLICT(uid) DO UPDATE SET
                        nombre=excluded.nombre, email=excluded.email, unit=excluded.unit,
                        status=excluded.status, fcm_token=excluded.fcm_token, synced_at=excluded.synced_at
                    """,
                    {
                        "uid": r.get("uid"),
                        "nombre": r.get("nombre", "Sin nombre"),
                        "email": r.get("email", ""),
                        "unit": str(r.get("unit", "")),
                        "status": r.get("status", "Activo"),
                        "fcm_token": r.get("fcm_token", ""),
                        "synced_at": ahora,
                    },
                )
        logger.info("Caché de residentes actualizada (%s registros).", len(residentes))

    def get_residentes_por_unidad(self, unit: str, solo_activos: bool = True) -> list:
        """Residentes de una unidad (para la pantalla de selección de nombre)."""
        q = "SELECT * FROM residentes WHERE unit = ?"
        params = [str(unit)]
        if solo_activos:
            q += " AND status = 'Activo'"
        q += " ORDER BY nombre"
        with self._lock:
            filas = self._conn.execute(q, params).fetchall()
        return [
            {"uid": f["uid"], "nombre": f["nombre"], "email": f["email"],
             "unit": f["unit"], "fcm_token": f["fcm_token"]}
            for f in filas
        ]

    def contar_residentes(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM residentes").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Couriers (caché desde Firebase 'config/courierCompanies')
    # ------------------------------------------------------------------ #
    def guardar_couriers(self, nombres: list):
        """Reemplaza la caché local de couriers con la lista del backend."""
        ahora = _ahora_iso()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM couriers")   # refleja también las bajas
            for i, nombre in enumerate(nombres):
                self._conn.execute(
                    "INSERT OR REPLACE INTO couriers (nombre, orden, synced_at) VALUES (?, ?, ?)",
                    (nombre, i, ahora),
                )
        logger.info("Caché de couriers actualizada (%s).", len(nombres))

    def get_couriers(self) -> list:
        with self._lock:
            filas = self._conn.execute(
                "SELECT nombre FROM couriers ORDER BY orden"
            ).fetchall()
        return [f["nombre"] for f in filas]

    # ------------------------------------------------------------------ #
    # Config del kiosco (caché desde Firebase 'kiosks/{kiosk_id}')
    # ------------------------------------------------------------------ #
    def guardar_kiosk_config(self, kiosk_id: str, data: dict):
        """Cachea la config lógica remota del equipo (se aplica al próximo inicio)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO kiosk_config (kiosk_id, data, synced_at) VALUES (?, ?, ?)",
                (kiosk_id, json.dumps(data), _ahora_iso()),
            )
        logger.info("Config remota del kiosco %s cacheada.", kiosk_id)

    def get_kiosk_config(self) -> dict | None:
        """Devuelve la última config remota cacheada (o None si no hay)."""
        with self._lock:
            fila = self._conn.execute(
                "SELECT data FROM kiosk_config ORDER BY synced_at DESC LIMIT 1"
            ).fetchone()
        if not fila:
            return None
        try:
            return json.loads(fila["data"])
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Encomiendas (locales)
    # ------------------------------------------------------------------ #
    def crear_encomienda(self, datos: dict) -> str:
        """
        Crea una encomienda LOCAL y devuelve su parcel_id (UUID = QR = doc-id).

        `datos` esperado:
            condo_id, condo_name, unit, resident_name, resident_user_id,
            tamano, locker_id, tipo_recurso, courier (opcional), created_by_name
        """
        parcel_id = uuid.uuid4().hex  # 32 hex chars, válido como doc-id de Firestore
        ahora = _ahora_iso()
        registro = {
            "parcel_id": parcel_id,
            "condo_id": datos.get("condo_id", ""),
            "condo_name": datos.get("condo_name", ""),
            "unit": str(datos.get("unit", "")),
            "resident_name": datos.get("resident_name", ""),
            "resident_user_id": datos.get("resident_user_id", ""),
            "tamano": datos.get("tamano", ""),
            "locker_id": datos.get("locker_id", ""),
            "tipo_recurso": datos.get("tipo_recurso", ""),
            "courier": datos.get("courier", ""),
            "status": "pending",
            "arrived_at": ahora,
            "picked_up_at": None,
            "created_by_name": datos.get("created_by_name", "Kiosco"),
            "created_at": ahora,
        }
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO encomiendas (
                    parcel_id, condo_id, condo_name, unit, resident_name, resident_user_id,
                    tamano, locker_id, tipo_recurso, courier, status, arrived_at, picked_up_at,
                    created_by_name, remote_creado, sync_status, sync_intentos, created_at
                ) VALUES (
                    :parcel_id, :condo_id, :condo_name, :unit, :resident_name, :resident_user_id,
                    :tamano, :locker_id, :tipo_recurso, :courier, :status, :arrived_at, :picked_up_at,
                    :created_by_name, 0, 'pendiente', 0, :created_at
                )
                """,
                registro,
            )
        logger.info("Encomienda local creada %s (unidad %s, locker %s).",
                    parcel_id, registro["unit"], registro["locker_id"])
        return parcel_id

    def get_encomienda(self, parcel_id: str) -> dict | None:
        with self._lock:
            fila = self._conn.execute(
                "SELECT * FROM encomiendas WHERE parcel_id = ?", (parcel_id,)
            ).fetchone()
        return dict(fila) if fila else None

    def get_encomienda_pendiente_por_id(self, parcel_id: str) -> dict | None:
        """Busca una encomienda EN portería (status 'pending') por su ID (retiro)."""
        with self._lock:
            fila = self._conn.execute(
                "SELECT * FROM encomiendas WHERE parcel_id = ? AND status = 'pending'",
                (parcel_id,),
            ).fetchone()
        return dict(fila) if fila else None

    def marcar_retirada(self, parcel_id: str) -> bool:
        """Marca la encomienda como retirada y la deja pendiente de re-sync."""
        ahora = _ahora_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE encomiendas
                   SET status = 'picked_up', picked_up_at = ?, sync_status = 'pendiente'
                 WHERE parcel_id = ? AND status = 'pending'
                """,
                (ahora, parcel_id),
            )
        ok = cur.rowcount > 0
        if ok:
            logger.info("Encomienda %s marcada como retirada.", parcel_id)
        return ok

    def lockers_ocupados(self) -> list:
        """IDs de recursos (locker/buzón) con una encomienda pendiente de retiro."""
        with self._lock:
            filas = self._conn.execute(
                "SELECT DISTINCT locker_id FROM encomiendas "
                "WHERE status = 'pending' AND locker_id != ''"
            ).fetchall()
        return [f["locker_id"] for f in filas]

    # ------------------------------------------------------------------ #
    # Sincronización (usado por SyncService)
    # ------------------------------------------------------------------ #
    def get_pendientes_sync(self) -> list:
        """Encomiendas que aún deben empujarse a Firebase (crear o actualizar)."""
        with self._lock:
            filas = self._conn.execute(
                "SELECT * FROM encomiendas WHERE sync_status = 'pendiente' "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(f) for f in filas]

    def marcar_sincronizada(self, parcel_id: str, remote_creado: bool = True):
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE encomiendas
                   SET sync_status = 'sincronizado', remote_creado = ?, last_error = NULL
                 WHERE parcel_id = ?
                """,
                (1 if remote_creado else 0, parcel_id),
            )
        logger.info("Encomienda %s sincronizada.", parcel_id)

    def registrar_error_sync(self, parcel_id: str, error: str, max_reintentos: int = 5):
        with self._lock, self._conn:
            fila = self._conn.execute(
                "SELECT sync_intentos FROM encomiendas WHERE parcel_id = ?", (parcel_id,)
            ).fetchone()
            intentos = (fila["sync_intentos"] if fila else 0) + 1
            nuevo_estado = "error" if intentos >= max_reintentos else "pendiente"
            self._conn.execute(
                """
                UPDATE encomiendas
                   SET sync_intentos = ?, last_error = ?, sync_status = ?
                 WHERE parcel_id = ?
                """,
                (intentos, str(error)[:500], nuevo_estado, parcel_id),
            )
        logger.warning("Error de sync en %s (intento %s): %s", parcel_id, intentos, error)

    def contar_pendientes_sync(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM encomiendas WHERE sync_status != 'sincronizado'"
            ).fetchone()[0]

    # ------------------------------------------------------------------ #
    def close(self):
        with self._lock:
            self._conn.close()
        logger.info("LocalStore cerrado.")


# ---------------------------------------------------------------------------
# Prueba rápida: python local_store.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    store = LocalStore(":memory:")

    # Simular residentes descargados de Firebase.
    store.upsert_residentes([
        {"uid": "u1", "nombre": "Juan Pérez", "email": "juan@x.cl", "unit": "101", "status": "Activo"},
        {"uid": "u2", "nombre": "María López", "email": "maria@x.cl", "unit": "101", "status": "Activo"},
        {"uid": "u3", "nombre": "Inactivo Test", "email": "z@x.cl", "unit": "101", "status": "Inactivo"},
    ])
    print("Residentes activos unidad 101:", store.get_residentes_por_unidad("101"))

    pid = store.crear_encomienda({
        "condo_id": "condo_demo", "condo_name": "Demo", "unit": "101",
        "resident_name": "Juan Pérez", "resident_user_id": "u1",
        "tamano": "mediana", "locker_id": "L1", "tipo_recurso": "locker",
    })
    print("Encomienda creada, QR/ID =", pid)
    print("Lockers ocupados:", store.lockers_ocupados())
    print("Pendientes de sync:", store.contar_pendientes_sync())

    store.marcar_sincronizada(pid)
    print("Tras sync, pendientes:", store.contar_pendientes_sync())

    store.marcar_retirada(pid)
    print("Tras retiro -> pendiente de re-sync:", store.contar_pendientes_sync())
    print("Lockers ocupados tras retiro:", store.lockers_ocupados())
