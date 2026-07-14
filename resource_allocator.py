"""
resource_allocator.py
----------------------
Decide qué recurso (locker o buzón) asignar según el tamaño de la encomienda
y la disponibilidad.

Reglas de negocio (sistema mixto):
    - 'chica'   -> buzón
    - 'mediana' -> locker mediano (fallback: grande)
    - 'grande'  -> locker grande

La ocupación se mantiene en memoria. En producción conviene sincronizarla con
Firestore (colección 'entregas' con estado 'depositada') al iniciar la app.
"""

import logging

logger = logging.getLogger(__name__)


class SinDisponibilidadError(Exception):
    """No hay ningún recurso libre para el tamaño solicitado."""


class ResourceAllocator:

    def __init__(self, config_manager, local_store=None):
        self.cm = config_manager
        # LocalStore es la fuente de verdad de la ocupación (encomiendas 'pending').
        self.local_store = local_store
        # Set de ids ocupados (lockers y buzón).
        self._ocupados = set()
        self.refrescar_ocupacion()

    # ------------------------------------------------------------------ #
    def marcar_ocupado(self, recurso_id: str):
        self._ocupados.add(recurso_id)

    def liberar(self, recurso_id: str):
        self._ocupados.discard(recurso_id)

    def sincronizar_ocupados(self, ids_ocupados):
        """Reemplaza el estado de ocupación con una lista dada."""
        self._ocupados = set(ids_ocupados)

    def refrescar_ocupacion(self):
        """Recarga la ocupación desde el LocalStore (encomiendas en portería)."""
        if self.local_store is not None:
            self._ocupados = set(self.local_store.lockers_ocupados())

    # ------------------------------------------------------------------ #
    def asignar(self, tamano: str) -> dict:
        """
        Devuelve el recurso asignado como dict:
            { "id", "tipo": "locker"|"buzon", "tamano" }

        Raises:
            SinDisponibilidadError: si no hay recurso libre.
            ValueError: si el tamaño no es válido para esta configuración.
        """
        # Partir del estado real (encomiendas actualmente en portería).
        self.refrescar_ocupacion()

        # Chica -> buzón; cualquier otro tamaño -> un locker libre.
        if tamano == "chica":
            return self._asignar_buzon()
        return self._asignar_locker()

    # ------------------------------------------------------------------ #
    def _asignar_buzon(self) -> dict:
        buzon = self.cm.get_buzon()
        if not buzon:
            raise ValueError("Este sistema no tiene buzón para encomiendas chicas.")
        if buzon["id"] in self._ocupados:
            raise SinDisponibilidadError("El buzón está ocupado.")
        self.marcar_ocupado(buzon["id"])
        logger.info("Buzón %s asignado.", buzon["id"])
        return {"id": buzon["id"], "tipo": "buzon", "tamano": "chica"}

    def _asignar_locker(self) -> dict:
        """Asigna el primer locker libre (todos son del mismo tamaño)."""
        if not self.cm.tiene_lockers():
            raise ValueError("Este sistema no tiene lockers.")

        for lk in self.cm.get_lockers():
            if lk["id"] not in self._ocupados:
                self.marcar_ocupado(lk["id"])
                logger.info("Locker %s asignado.", lk["id"])
                return {"id": lk["id"], "tipo": "locker", "tamano": lk.get("tamano")}

        raise SinDisponibilidadError("No hay lockers disponibles.")
