"""
hardware_manager.py
-------------------
Encapsula toda la lógica de bajo nivel de GPIO (relés / cerraduras
electromagnéticas) del sistema "Portería Virtual".

Diseño clave:
- Capa de abstracción GPIO: en la Raspberry Pi usa `RPi.GPIO`; en un PC de
  desarrollo (Windows/Mac) carga automáticamente un *mock* que imprime las
  acciones por consola. Así se puede probar toda la lógica sin hardware.
- Soporta las modalidades 'unidireccional' (1 puerta) y 'bidireccional'
  (2 puertas) según el `config.json`.
- El buzón especial siempre tiene 2 cerraduras físicas (depósito y retiro).
"""

import time
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capa de abstracción GPIO
# ---------------------------------------------------------------------------
def _cargar_gpio():
    """
    Intenta cargar la librería real de la Raspberry Pi. Si no está disponible
    (ej. desarrollo en Windows), devuelve un mock que registra las acciones.

    Retorna: (modulo_gpio, es_simulado: bool)
    """
    try:
        import RPi.GPIO as GPIO  # type: ignore
        return GPIO, False
    except (ImportError, RuntimeError):
        logger.warning("RPi.GPIO no disponible. Usando GPIO SIMULADO (mock).")
        return _MockGPIO(), True


class _MockGPIO:
    """Simula la interfaz de RPi.GPIO para desarrollo fuera de la Raspberry."""

    BCM = "BCM"
    BOARD = "BOARD"
    OUT = "OUT"
    IN = "IN"
    HIGH = 1
    LOW = 0

    def setmode(self, modo):
        logger.info("[MOCK GPIO] setmode(%s)", modo)

    def setwarnings(self, estado):
        logger.info("[MOCK GPIO] setwarnings(%s)", estado)

    def setup(self, pin, modo, initial=None):
        logger.info("[MOCK GPIO] setup(pin=%s, modo=%s, initial=%s)", pin, modo, initial)

    def output(self, pin, estado):
        nivel = "HIGH" if estado else "LOW"
        logger.info("[MOCK GPIO] output(pin=%s, %s)", pin, nivel)

    def cleanup(self):
        logger.info("[MOCK GPIO] cleanup()")


# ---------------------------------------------------------------------------
# HardwareManager
# ---------------------------------------------------------------------------
class HardwareManager:
    """
    Controla las cerraduras físicas mapeadas a pines GPIO.

    Uso típico:
        hw = HardwareManager(config)          # config = dict del config.json
        hw.abrir_cerradura("L1", "deposito")  # abre locker 1 para dejar
        hw.abrir_cerradura("B1", "retiro")    # abre buzón para retirar
        hw.cleanup()                          # al cerrar la app
    """

    OPERACIONES_VALIDAS = ("deposito", "retiro")

    def __init__(self, config: dict):
        self.config = config

        cfg_gpio = config.get("gpio", {})
        self.modo_numeracion = cfg_gpio.get("modo", "BCM")
        self.activo_en_bajo = cfg_gpio.get("rele_activo_en_bajo", True)
        self.duracion_pulso = cfg_gpio.get("duracion_pulso_seg", 1.5)

        cfg_sistema = config.get("sistema", {})
        # 'unidireccional' | 'bidireccional'
        self.operacion_puertas = cfg_sistema.get("operacion_puertas", "unidireccional")

        # Diccionario interno: { id_recurso: {"deposito": pin, "retiro": pin, "tamano": str} }
        self._recursos = {}

        self.GPIO, self.es_simulado = _cargar_gpio()

        self._construir_mapa_recursos()
        self._inicializar_gpio()

    # ------------------------------------------------------------------ #
    # Construcción del mapa de recursos desde la configuración
    # ------------------------------------------------------------------ #
    def _construir_mapa_recursos(self):
        """Aplana lockers + buzón del config en un solo diccionario por id."""
        recursos_cfg = self.config.get("recursos", {})

        for locker in recursos_cfg.get("lockers", []):
            self._registrar_recurso(locker, es_buzon=False)

        buzon = recursos_cfg.get("buzon")
        if buzon:
            # El buzón SIEMPRE es bidireccional físicamente (2 cerraduras),
            # independiente de la modalidad general del sistema.
            self._registrar_recurso(buzon, es_buzon=True)

        logger.info("Recursos mapeados: %s", list(self._recursos.keys()))

    def _registrar_recurso(self, recurso: dict, es_buzon: bool):
        rid = recurso["id"]
        pin_deposito = recurso.get("pin_deposito")
        pin_retiro = recurso.get("pin_retiro")

        # En modalidad unidireccional (solo lockers), retiro y depósito
        # comparten la misma puerta física -> mismo pin.
        # El buzón es la excepción: siempre usa dos pines distintos.
        if not es_buzon and self.operacion_puertas == "unidireccional":
            pin_retiro = pin_deposito

        self._recursos[rid] = {
            "deposito": pin_deposito,
            "retiro": pin_retiro,
            "tamano": recurso.get("tamano"),
            "es_buzon": es_buzon,
        }

    # ------------------------------------------------------------------ #
    # Inicialización de pines GPIO
    # ------------------------------------------------------------------ #
    def _inicializar_gpio(self):
        modo = self.GPIO.BCM if self.modo_numeracion == "BCM" else self.GPIO.BOARD
        self.GPIO.setmode(modo)
        self.GPIO.setwarnings(False)

        # Estado de reposo: cerradura NO accionada.
        estado_reposo = self.GPIO.HIGH if self.activo_en_bajo else self.GPIO.LOW

        pines_configurados = set()
        for rid, datos in self._recursos.items():
            for operacion in self.OPERACIONES_VALIDAS:
                pin = datos.get(operacion)
                if pin is not None and pin not in pines_configurados:
                    self.GPIO.setup(pin, self.GPIO.OUT, initial=estado_reposo)
                    pines_configurados.add(pin)

        logger.info(
            "GPIO inicializado (%s pines) | modo=%s | simulado=%s",
            len(pines_configurados), self.modo_numeracion, self.es_simulado,
        )

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def abrir_cerradura(self, id_recurso: str, operacion: str) -> bool:
        """
        Acciona la cerradura correspondiente a un recurso.

        Args:
            id_recurso: ej. "L1", "B1".
            operacion: "deposito" o "retiro".

        Returns:
            True si se accionó correctamente.

        Raises:
            ValueError: si la operación o el recurso no son válidos, o si el
                        recurso no tiene una puerta para esa operación.
        """
        if operacion not in self.OPERACIONES_VALIDAS:
            raise ValueError(
                f"Operación inválida: '{operacion}'. "
                f"Use una de {self.OPERACIONES_VALIDAS}."
            )

        recurso = self._recursos.get(id_recurso)
        if recurso is None:
            raise ValueError(f"Recurso desconocido: '{id_recurso}'.")

        pin = recurso.get(operacion)
        if pin is None:
            raise ValueError(
                f"El recurso '{id_recurso}' no tiene cerradura para '{operacion}'."
            )

        logger.info(
            "Accionando cerradura | recurso=%s | operacion=%s | pin=%s",
            id_recurso, operacion, pin,
        )
        self._pulsar(pin)
        return True

    def cleanup(self):
        """Libera los recursos GPIO. Llamar al cerrar la aplicación."""
        try:
            self.GPIO.cleanup()
            logger.info("GPIO liberado (cleanup).")
        except Exception as exc:  # noqa: BLE001
            logger.error("Error en cleanup de GPIO: %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers internos
    # ------------------------------------------------------------------ #
    def _pulsar(self, pin: int):
        """Acciona un relé durante `duracion_pulso` segundos y lo devuelve a reposo."""
        nivel_activo = self.GPIO.LOW if self.activo_en_bajo else self.GPIO.HIGH
        nivel_reposo = self.GPIO.HIGH if self.activo_en_bajo else self.GPIO.LOW

        self.GPIO.output(pin, nivel_activo)
        time.sleep(self.duracion_pulso)
        self.GPIO.output(pin, nivel_reposo)

    # Permite usar la clase con 'with HardwareManager(cfg) as hw:'
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


# ---------------------------------------------------------------------------
# Prueba manual rápida (ejecutar: python hardware_manager.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    with HardwareManager(cfg) as hw:
        print("\n--- Prueba: depósito en locker L1 ---")
        hw.abrir_cerradura("L1", "deposito")

        print("\n--- Prueba: retiro en buzón B1 ---")
        hw.abrir_cerradura("B1", "retiro")

        print("\n--- Prueba: operación inválida (debe fallar) ---")
        try:
            hw.abrir_cerradura("L1", "abrir")
        except ValueError as e:
            print(f"OK, error esperado: {e}")
