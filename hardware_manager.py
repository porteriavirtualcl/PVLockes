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
- Además de las cerraduras, maneja la PUERTA DE ACCESO A LA SALA donde están
  instalados los lockers (sección 'puerta_sala' del config). Se libera en
  PARALELO con la cerradura del locker: su pulso corre en un hilo aparte para
  no congelar la GUI, porque dura bastante más (10 s) que el de un locker.
"""

import threading
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

        # --- Puerta de acceso a la sala de lockers (electroimán) -----------
        cfg_puerta = config.get("puerta_sala") or {}
        self.puerta_habilitada = bool(cfg_puerta.get("habilitado", False))
        self.puerta_pin = cfg_puerta.get("pin")
        self.puerta_duracion = float(cfg_puerta.get("duracion_pulso_seg", 10))
        # Un electroimán se cablea al contacto NC y se suelta al accionar el
        # relé, igual que una cerradura activa en bajo: por defecto hereda la
        # lógica global, pero se puede invertir si se usa un pestillo distinto.
        # Ojo: la clave puede venir presente y en null ("hereda"); por eso no
        # sirve el default de .get() y hay que comparar explícitamente con None.
        _puerta_aeb = cfg_puerta.get("activo_en_bajo")
        self.puerta_activo_en_bajo = (self.activo_en_bajo if _puerta_aeb is None
                                      else bool(_puerta_aeb))
        # Evita que dos depósitos seguidos peleen por el mismo pin.
        self._puerta_ocupada = threading.Lock()

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

        # La puerta de la sala usa su propio pin y su propio nivel de reposo.
        # Si choca con una cerradura, se deshabilita en vez de romper el arranque.
        if self.puerta_habilitada:
            if self.puerta_pin is None:
                logger.error("puerta_sala habilitada sin 'pin'; queda deshabilitada.")
                self.puerta_habilitada = False
            elif self.puerta_pin in pines_configurados:
                logger.error(
                    "El pin %s de puerta_sala ya está usado por una cerradura; "
                    "la puerta queda deshabilitada.", self.puerta_pin,
                )
                self.puerta_habilitada = False
            else:
                reposo_puerta = (self.GPIO.HIGH if self.puerta_activo_en_bajo
                                 else self.GPIO.LOW)
                self.GPIO.setup(self.puerta_pin, self.GPIO.OUT, initial=reposo_puerta)
                pines_configurados.add(self.puerta_pin)
                logger.info(
                    "Puerta de sala en pin %s | pulso=%.1f s | activo_en_bajo=%s",
                    self.puerta_pin, self.puerta_duracion, self.puerta_activo_en_bajo,
                )

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
        pin = self._resolver_pin(id_recurso, operacion)
        logger.info(
            "Accionando cerradura | recurso=%s | operacion=%s | pin=%s",
            id_recurso, operacion, pin,
        )
        self._pulsar(pin)
        return True

    def abrir_puerta_sala(self) -> bool:
        """
        Libera la puerta de acceso a la sala de lockers.

        NO BLOQUEA: el pulso (10 s por defecto) corre en un hilo aparte, así la
        GUI sigue respondiendo y la puerta queda liberada en paralelo con la
        cerradura del locker.

        Returns:
            True si se lanzó el pulso; False si la puerta está deshabilitada o
            ya hay un pulso en curso.
        """
        if not self.puerta_habilitada or self.puerta_pin is None:
            logger.info("Puerta de sala deshabilitada; no se acciona.")
            return False

        # Un pulso en curso ya tiene la puerta liberada: no se relanza.
        if not self._puerta_ocupada.acquire(blocking=False):
            logger.info("Puerta de sala ya liberada; se ignora el pedido duplicado.")
            return False

        def _tarea():
            try:
                logger.info(
                    "Liberando puerta de sala | pin=%s | %.1f s",
                    self.puerta_pin, self.puerta_duracion,
                )
                self._pulsar(self.puerta_pin, self.puerta_duracion,
                             self.puerta_activo_en_bajo)
                logger.info("Puerta de sala enclavada de nuevo.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Error accionando la puerta de sala: %s", exc)
            finally:
                self._puerta_ocupada.release()

        threading.Thread(target=_tarea, name="puerta-sala", daemon=True).start()
        return True

    def abrir_con_acceso_sala(self, id_recurso: str, operacion: str) -> bool:
        """
        Abre la cerradura del locker Y la puerta de la sala AL MISMO TIEMPO.

        Es lo que corresponde cuando se asigna un locker: el repartidor necesita
        entrar a la sala y encontrar la puerta del casillero ya abierta.

        El pin del locker se valida ANTES de liberar la puerta, para no dejar la
        sala abierta si el recurso está mal configurado.

        Returns:
            True si la cerradura del locker se accionó (la puerta de la sala es
            complementaria: si está deshabilitada o falla, el depósito sigue).

        Raises:
            ValueError: si la operación o el recurso no son válidos.
        """
        pin = self._resolver_pin(id_recurso, operacion)

        # 1) La puerta primero: vuelve de inmediato y su pulso corre en paralelo.
        self.abrir_puerta_sala()

        # 2) La cerradura del locker, con su propio pulso corto.
        logger.info(
            "Accionando cerradura | recurso=%s | operacion=%s | pin=%s (+ puerta de sala)",
            id_recurso, operacion, pin,
        )
        self._pulsar(pin)
        return True

    def abrir_todas(self) -> tuple[int, list[str]]:
        """
        Abre TODAS las cerraduras de todos los recursos (ambos lados), en forma
        SECUENCIAL. Es un override manual del super administrador.

        Secuencial a propósito: pulsar 12 solenoides a la vez dispararía una
        punta de corriente grande sobre la fuente de 12 V. Cada cerradura usa su
        pulso corto normal y se libera una tras otra.

        Returns:
            (aperturas_ok, errores): cuántas se accionaron y la lista de fallos.
        """
        ok = 0
        errores: list[str] = []
        vistos: set[int] = set()  # en unidireccional ambos lados comparten pin
        for id_recurso, puertas in self._recursos.items():
            for operacion in self.OPERACIONES_VALIDAS:
                pin = puertas.get(operacion)
                if pin is None or pin in vistos:
                    continue
                vistos.add(pin)
                try:
                    logger.info("Apertura masiva | recurso=%s | %s | pin=%s",
                                id_recurso, operacion, pin)
                    self._pulsar(pin)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    errores.append(f"{id_recurso}/{operacion}: {e}")
        logger.info("Apertura masiva: %s cerradura(s) accionada(s), %s error(es).",
                    ok, len(errores))
        return ok, errores

    def cleanup(self):
        """Libera los recursos GPIO. Llamar al cerrar la aplicación."""
        # Si la app se cierra en medio de un pulso, el pin quedaría flotando y
        # el electroimán podría no volver a enclavar: se fuerza el reposo.
        if self.puerta_habilitada and self.puerta_pin is not None:
            try:
                nivel_reposo = (self.GPIO.HIGH if self.puerta_activo_en_bajo
                                else self.GPIO.LOW)
                self.GPIO.output(self.puerta_pin, nivel_reposo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo enclavar la puerta de sala: %s", exc)
        try:
            self.GPIO.cleanup()
            logger.info("GPIO liberado (cleanup).")
        except Exception as exc:  # noqa: BLE001
            logger.error("Error en cleanup de GPIO: %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers internos
    # ------------------------------------------------------------------ #
    def _resolver_pin(self, id_recurso: str, operacion: str) -> int:
        """Valida recurso + operación y devuelve el pin, sin accionar nada."""
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
        return pin

    def _pulsar(self, pin: int, duracion: float = None, activo_en_bajo: bool = None):
        """
        Acciona un relé durante `duracion` segundos y lo devuelve a reposo.

        `duracion` y `activo_en_bajo` por defecto toman los valores globales de
        la sección 'gpio'; la puerta de la sala pasa los suyos.
        """
        if duracion is None:
            duracion = self.duracion_pulso
        if activo_en_bajo is None:
            activo_en_bajo = self.activo_en_bajo

        nivel_activo = self.GPIO.LOW if activo_en_bajo else self.GPIO.HIGH
        nivel_reposo = self.GPIO.HIGH if activo_en_bajo else self.GPIO.LOW

        self.GPIO.output(pin, nivel_activo)
        time.sleep(duracion)
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

        print("\n--- Prueba: L1 + puerta de la sala en paralelo ---")
        hw.abrir_con_acceso_sala("L1", "deposito")
        # El pulso de la puerta corre en su hilo: se espera a que termine para
        # que la prueba no haga cleanup con la puerta liberada.
        time.sleep(hw.puerta_duracion + 0.5)

        print("\n--- Prueba: retiro en buzón B1 ---")
        hw.abrir_cerradura("B1", "retiro")

        print("\n--- Prueba: operación inválida (debe fallar) ---")
        try:
            hw.abrir_cerradura("L1", "abrir")
        except ValueError as e:
            print(f"OK, error esperado: {e}")
