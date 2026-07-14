"""
config_manager.py
-----------------
Carga y valida el archivo `config.json` del sistema "Portería Virtual".

Responsabilidades:
- Leer el JSON y validar estructura, tipos y coherencia.
- Detectar errores de configuración temprano (pines duplicados, tipo de
  sistema inválido, buzón sin 2 cerraduras, etc.) antes de tocar el hardware.
- Exponer helpers para el resto de la app (¿hay buzón?, ¿hay lockers?,
  lockers por tamaño, etc.).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Error de configuración: el config.json es inválido o incoherente."""


class ConfigManager:
    TIPOS_SISTEMA = ("locker", "buzon", "mixto")
    MODOS_OPERACION = ("unidireccional", "bidireccional")
    TAMANOS = ("chica", "mediana", "grande")
    TIPOS_UNIDAD = ("departamento", "casa")

    # Etiqueta que ve el repartidor según el tipo de unidad.
    ETIQUETAS_UNIDAD = {"departamento": "N° Depto", "casa": "N° Casa"}

    def __init__(self, ruta_config: str = "config.json"):
        self.ruta_config = ruta_config
        self.config = self._cargar()
        self._validar()

    # ------------------------------------------------------------------ #
    # Carga
    # ------------------------------------------------------------------ #
    def _cargar(self) -> dict:
        try:
            with open(self.ruta_config, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise ConfigError(f"No se encontró el archivo de configuración: {self.ruta_config}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"El config.json tiene un error de sintaxis: {e}")

    # ------------------------------------------------------------------ #
    # Validación
    # ------------------------------------------------------------------ #
    def _validar(self):
        sistema = self.config.get("sistema", {})
        tipo = sistema.get("tipo")
        operacion = sistema.get("operacion_puertas")

        if tipo not in self.TIPOS_SISTEMA:
            raise ConfigError(
                f"Tipo de sistema inválido: '{tipo}'. Debe ser uno de {self.TIPOS_SISTEMA}."
            )
        if operacion not in self.MODOS_OPERACION:
            raise ConfigError(
                f"Operación de puertas inválida: '{operacion}'. "
                f"Debe ser una de {self.MODOS_OPERACION}."
            )

        tipo_unidad = sistema.get("tipo_unidad", "departamento")
        if tipo_unidad not in self.TIPOS_UNIDAD:
            raise ConfigError(
                f"Tipo de unidad inválido: '{tipo_unidad}'. "
                f"Debe ser uno de {self.TIPOS_UNIDAD}."
            )

        recursos = self.config.get("recursos", {})
        lockers = recursos.get("lockers", [])
        buzon = recursos.get("buzon")

        # Coherencia entre tipo de sistema y recursos declarados.
        if tipo in ("locker", "mixto") and not lockers:
            raise ConfigError(f"El tipo '{tipo}' requiere al menos un locker definido.")
        if tipo in ("buzon", "mixto") and not buzon:
            raise ConfigError(f"El tipo '{tipo}' requiere un buzón definido.")
        if tipo == "locker" and buzon:
            logger.warning("Tipo 'locker' pero hay un buzón definido; será ignorado.")
        if tipo == "buzon" and lockers:
            logger.warning("Tipo 'buzon' pero hay lockers definidos; serán ignorados.")

        # El buzón siempre necesita 2 cerraduras físicas distintas.
        if buzon and tipo in ("buzon", "mixto"):
            if buzon.get("pin_deposito") is None or buzon.get("pin_retiro") is None:
                raise ConfigError("El buzón debe definir 'pin_deposito' y 'pin_retiro'.")
            if buzon["pin_deposito"] == buzon["pin_retiro"]:
                raise ConfigError("El buzón requiere 2 pines DISTINTOS (depósito y retiro).")

        # Validar tamaños de locker.
        for lk in lockers:
            if lk.get("tamano") not in self.TAMANOS:
                raise ConfigError(
                    f"Locker '{lk.get('id')}' tiene tamaño inválido: '{lk.get('tamano')}'."
                )

        self._validar_pines_unicos(lockers, buzon, operacion, tipo)
        logger.info("Configuración validada correctamente (tipo=%s, operacion=%s).", tipo, operacion)

    def _validar_pines_unicos(self, lockers, buzon, operacion, tipo):
        """Ningún pin GPIO puede estar asignado a dos cerraduras distintas."""
        pines = []

        for lk in lockers:
            pines.append(lk.get("pin_deposito"))
            # En bidireccional, el pin de retiro también se usa.
            if operacion == "bidireccional":
                pines.append(lk.get("pin_retiro"))

        if buzon and tipo in ("buzon", "mixto"):
            pines.extend([buzon.get("pin_deposito"), buzon.get("pin_retiro")])

        pines = [p for p in pines if p is not None]
        duplicados = {p for p in pines if pines.count(p) > 1}
        if duplicados:
            raise ConfigError(f"Pines GPIO duplicados en la configuración: {sorted(duplicados)}")

    # ------------------------------------------------------------------ #
    # Config remota (Firestore kiosks/{kiosk_id}) — lógica remota + pines locales
    # ------------------------------------------------------------------ #
    def aplicar_config_remota(self, remoto: dict | None):
        """
        Aplica la configuración lógica descargada del backend sobre la local.

        Los PINES siempre vienen del `config.json` local (físico). Lo remoto
        define tipo, operación, orientación, retiro automático, y QUÉ recursos
        existen con sus tamaños. Si algo falla, se conserva la config local.
        """
        if not remoto:
            logger.info("Sin config remota; se usa la configuración local (fallback).")
            return

        # Guardar una copia por si hay que revertir ante un merge inválido.
        import copy
        respaldo = copy.deepcopy(self.config)
        try:
            sistema = self.config.setdefault("sistema", {})
            if remoto.get("tipo"):
                sistema["tipo"] = remoto["tipo"]
            if remoto.get("operacionPuertas"):
                sistema["operacion_puertas"] = remoto["operacionPuertas"]
            if remoto.get("tipoUnidad"):
                sistema["tipo_unidad"] = remoto["tipoUnidad"]
            if remoto.get("orientacion"):
                self.config.setdefault("pantalla", {})["orientacion"] = remoto["orientacion"]
            if "retiroAutomatico" in remoto:
                self.config.setdefault("retiro_automatico", {})["habilitado"] = bool(remoto["retiroAutomatico"])
            cond = self.config.setdefault("condominio", {})
            if remoto.get("condoId"):
                cond["condo_id"] = remoto["condoId"]
            if remoto.get("condoName"):
                cond["condo_name"] = remoto["condoName"]

            self._fusionar_recursos(remoto)
            self._validar()  # revalida coherencia (pines únicos, tamaños, buzón 2 pines)
            logger.info("Config remota aplicada (tipo=%s, operacion=%s, orientacion=%s).",
                        self.tipo_sistema, self.operacion_puertas, self.orientacion)
        except Exception as e:  # noqa: BLE001
            self.config = respaldo
            logger.error("Config remota inválida (%s); se mantiene la configuración local.", e)

    def _fusionar_recursos(self, remoto: dict):
        """Cruza los pines locales (por id) con los lockers/buzón definidos en remoto."""
        recursos = self.config.get("recursos", {})
        locales_por_id = {lk["id"]: lk for lk in recursos.get("lockers", [])}
        buzon_local = recursos.get("buzon")

        nuevos_lockers = []
        for lk in remoto.get("lockers", []):
            lid = lk.get("id")
            base = locales_por_id.get(lid)
            if not base:
                logger.warning("Locker remoto '%s' sin pines locales; se omite.", lid)
                continue
            nuevos_lockers.append({**base, "tamano": lk.get("tamano", base.get("tamano"))})

        nuevo_buzon = None
        rb = remoto.get("buzon")
        if rb:
            if buzon_local:
                # Físicamente hay un solo buzón: se reusan sus pines, con id/tamaño remotos.
                nuevo_buzon = {**buzon_local,
                               "id": rb.get("id", buzon_local["id"]),
                               "tamano": rb.get("tamano", buzon_local.get("tamano"))}
            else:
                logger.warning("Buzón definido en remoto pero sin pines locales; se omite.")

        self.config["recursos"] = {"lockers": nuevos_lockers, "buzon": nuevo_buzon}

    # ------------------------------------------------------------------ #
    # Helpers de acceso
    # ------------------------------------------------------------------ #
    @property
    def tipo_sistema(self) -> str:
        return self.config["sistema"]["tipo"]

    @property
    def operacion_puertas(self) -> str:
        return self.config["sistema"]["operacion_puertas"]

    @property
    def nombre_sistema(self) -> str:
        return self.config["sistema"].get("nombre", "Portería Virtual")

    @property
    def tipo_unidad(self) -> str:
        return self.config["sistema"].get("tipo_unidad", "departamento")

    @property
    def etiqueta_unidad(self) -> str:
        """Texto para el repartidor: 'N° Depto' o 'N° Casa'."""
        return self.ETIQUETAS_UNIDAD.get(self.tipo_unidad, "N° Unidad")

    # --- Pantalla / orientación ---
    @property
    def orientacion(self) -> str:
        return self.config.get("pantalla", {}).get("orientacion", "vertical")

    @property
    def es_vertical(self) -> bool:
        return self.orientacion == "vertical"

    @property
    def pantalla_completa(self) -> bool:
        return bool(self.config.get("pantalla", {}).get("pantalla_completa", False))

    # --- Empresas de reparto (courier) ---
    @property
    def couriers(self) -> list:
        return self.config.get("couriers", ["Otro"])

    # --- Identidad del equipo ---
    @property
    def kiosk_id(self) -> str:
        return self.config.get("kiosk_id", "")

    # --- Condominio (para el esquema de producción) ---
    @property
    def condo_id(self) -> str:
        return self.config.get("condominio", {}).get("condo_id", "")

    @property
    def condo_name(self) -> str:
        return self.config.get("condominio", {}).get("condo_name", "")

    # --- Retiro automático (lector QR dedicado, sin pantalla) ---
    @property
    def retiro_auto_habilitado(self) -> bool:
        return bool(self.config.get("retiro_automatico", {}).get("habilitado", False))

    @property
    def retiro_auto_dispositivo(self) -> str:
        return self.config.get("retiro_automatico", {}).get("dispositivo", "")

    # --- Sincronización offline-first ---
    @property
    def db_local(self) -> str:
        return self.config.get("sincronizacion", {}).get("db_local", "porteria_local.db")

    @property
    def intervalo_sync_seg(self) -> int:
        return int(self.config.get("sincronizacion", {}).get("intervalo_sync_seg", 60))

    @property
    def max_reintentos(self) -> int:
        return int(self.config.get("sincronizacion", {}).get("max_reintentos", 5))

    def tiene_buzon(self) -> bool:
        return self.tipo_sistema in ("buzon", "mixto")

    def tiene_lockers(self) -> bool:
        return self.tipo_sistema in ("locker", "mixto")

    def es_mixto(self) -> bool:
        return self.tipo_sistema == "mixto"

    def get_lockers(self) -> list:
        if not self.tiene_lockers():
            return []
        return self.config["recursos"].get("lockers", [])

    def get_buzon(self) -> dict | None:
        if not self.tiene_buzon():
            return None
        return self.config["recursos"].get("buzon")

    def lockers_por_tamano(self, tamano: str) -> list:
        """Devuelve los lockers de un tamaño dado (ej. 'grande')."""
        return [lk for lk in self.get_lockers() if lk.get("tamano") == tamano]

    def as_dict(self) -> dict:
        """Config cruda, para pasar al HardwareManager."""
        return self.config


# ---------------------------------------------------------------------------
# Prueba rápida: python config_manager.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    cm = ConfigManager("config.json")
    print(f"Sistema: {cm.nombre_sistema}")
    print(f"Tipo: {cm.tipo_sistema} | Operación: {cm.operacion_puertas}")
    print(f"¿Tiene buzón?: {cm.tiene_buzon()} | ¿Tiene lockers?: {cm.tiene_lockers()}")
    print(f"Lockers grandes: {[lk['id'] for lk in cm.lockers_por_tamano('grande')]}")
