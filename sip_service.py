"""
sip_service.py
--------------
Módulo de llamada SIP del kiosco "Portería Virtual".

Permite que el kiosco LLAME a una central de conserjería / centro de monitoreo
por SIP. Caso objetivo: una Master Station Dahua (VTS) registrada en un servidor
DSS; el kiosco se registra como una extensión SIP en ese servidor y marca el
número de la central.

Backend: **baresip** (cliente SIP liviano para Linux), manejado desde Python con
**baresipy**. baresip trae el manejo de audio ALSA y el códec G.711 (el de
Dahua). Se instala en la Pi con:
    sudo apt install baresip
    pip install baresipy           (dentro del venv)

Diseño (coherente con el resto de servicios del proyecto):
- TOLERANTE A FALLOS: si `baresipy` no está instalado (ej. en Windows/Mac de
  desarrollo) o el SIP está deshabilitado en config.json, el servicio queda en
  modo NO DISPONIBLE y el kiosco funciona con normalidad.
- baresipy corre baresip en su propio hilo; los eventos (registro, llamada) se
  informan por el callback `on_estado(estado, detalle)`, que la GUI envuelve con
  `root.after(...)` para tocar Tkinter de forma segura.

Estados que informa on_estado(estado: str, detalle: str):
    "registrando"     - enviando REGISTER al servidor
    "registrado"      - registrado y listo para llamar
    "error_registro"  - no se pudo registrar (credenciales/red)
    "llamando"        - marcando a la central
    "timbrando"       - la central está sonando
    "en_llamada"      - audio establecido (conversación en curso)
    "colgado"         - llamada finalizada
    "fallo_llamada"   - la llamada no se pudo establecer / fue rechazada
    "no_disponible"   - baresipy no instalado o SIP deshabilitado

Uso:
    sip = SipService(cfg_dict, on_estado=cb)
    sip.iniciar()          # registra en segundo plano
    sip.llamar()           # llama a la central (destino de la config)
    sip.colgar()           # cuelga la llamada en curso
    sip.detener()          # baja la pila SIP (al cerrar la app)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# baresipy se importa de forma diferida y tolerante: si no está (Windows/Mac de
# desarrollo, o Pi sin instalar), el servicio queda "no disponible" pero NO
# rompe el arranque del kiosco.
try:
    from baresipy import BareSIP  # type: ignore
    _BARESIP_OK = True
except Exception as _e:  # noqa: BLE001
    BareSIP = object  # type: ignore
    _BARESIP_OK = False
    logger.info("baresipy no disponible (%s); el módulo SIP quedará inactivo.", _e)


class SipService:
    """Cliente SIP para llamar a la central. Envuelve baresip vía baresipy."""

    def __init__(self, sip_cfg: dict, on_estado=None):
        """
        :param sip_cfg: sección 'sip' del config.json (dict).
        :param on_estado: callback(estado: str, detalle: str). Se invoca desde el
                          hilo de baresip; la GUI debe reprogramarlo con after().
        """
        self.cfg = sip_cfg or {}
        self.on_estado = on_estado

        self.habilitado = bool(self.cfg.get("habilitado", False))
        self.disponible = _BARESIP_OK and self.habilitado

        self._phone = None
        self._estado = "no_disponible"
        self._anuncio_wav: str | None = None

    # ------------------------------------------------------------------ #
    # API pública (segura desde el hilo de la GUI)
    # ------------------------------------------------------------------ #
    def iniciar(self):
        """Arranca baresip y registra la cuenta (en segundo plano)."""
        if not self.habilitado:
            logger.info("SIP deshabilitado en config; no se inicia.")
            self._emitir("no_disponible", "SIP deshabilitado")
            return
        if not _BARESIP_OK:
            logger.warning("SIP habilitado pero baresipy no está instalado.")
            self._emitir("no_disponible", "baresipy no instalado")
            return
        if self._phone is not None:
            return

        servidor = self.cfg.get("servidor", "").strip()
        usuario = self.cfg.get("usuario", "").strip()
        password = self.cfg.get("password", "")
        transporte = self.cfg.get("transporte", "udp")
        puerto = self.cfg.get("puerto", 5060)
        # Modo TRUNK (autenticación por IP, ej. Netexplora): NO se registra.
        # El SBC autoriza por la IP de origen; un REGISTER daría 403 y los 403
        # repetidos gatillan el fail2ban del proveedor (ban silencioso de IP).
        trunk = bool(self.cfg.get("trunk", False))

        if not (servidor and usuario):
            self._emitir("error_registro", "Faltan credenciales SIP (servidor/usuario).")
            return

        # baresipy arma la cuenta como sip:usuario@gateway. Si el puerto no es el
        # estándar, se incluye en el gateway (host:puerto).
        gateway = servidor if int(puerto) == 5060 else f"{servidor}:{puerto}"
        if trunk:
            gateway += ";regint=0"   # crea la cuenta SIN registrar

        try:
            self._emitir("registrando", gateway)
            # block=False -> baresipy arranca su hilo y vuelve enseguida.
            self._phone = _Telefono(self, usuario, password, gateway,
                                    transport=transporte, trunk=trunk)
        except Exception as e:  # noqa: BLE001
            logger.error("Error iniciando baresip: %s", e)
            self._emitir("error_registro", str(e))
            self._phone = None

    def llamar(self, destino: str | None = None, anuncio_wav: str | None = None):
        """
        Llama al `destino` dado (número) o al de la config si no se indica.

        `anuncio_wav`: ruta a un WAV que se reproduce DENTRO de la llamada al
        contestar (ej. "tiene una llamada desde la portería, de parte de X");
        al terminar, baresip devuelve el audio al micrófono. El archivo puede
        estar aún generándose (TTS): se espera hasta ~8 s a que exista.
        """
        self._anuncio_wav = anuncio_wav
        if not self.disponible or self._phone is None:
            self._emitir("no_disponible", "SIP no disponible")
            return
        destino = (destino or self.cfg.get("destino", "")).strip()
        if not destino:
            self._emitir("fallo_llamada", "No hay 'destino' (número de la central) configurado.")
            return
        # URI explícita contra el servidor (necesario en modo trunk, inocuo con registro).
        if not destino.startswith("sip:"):
            servidor = self.cfg.get("servidor", "").strip()
            destino = f"sip:{destino}@{servidor}"
        try:
            self._phone.call(destino)
            self._emitir("llamando", destino)
        except Exception as e:  # noqa: BLE001
            logger.error("Error al marcar: %s", e)
            self._emitir("fallo_llamada", str(e))

    def colgar(self):
        """Cuelga la llamada en curso (si hay)."""
        if self._phone is None:
            return
        try:
            self._phone.hang()
        except Exception as e:  # noqa: BLE001
            logger.warning("Error al colgar: %s", e)

    def detener(self):
        """Baja baresip ordenadamente (llamar al cerrar la app)."""
        if self._phone is None:
            return
        try:
            self._phone.quit()
        except Exception as e:  # noqa: BLE001
            logger.warning("Error bajando baresip: %s", e)
        finally:
            self._phone = None

    @property
    def estado(self) -> str:
        return self._estado

    # ------------------------------------------------------------------ #
    def _emitir(self, estado: str, detalle: str = ""):
        self._estado = estado
        logger.info("SIP estado=%s %s", estado, f"({detalle})" if detalle else "")
        if self.on_estado:
            try:
                self.on_estado(estado, detalle)
            except Exception as e:  # noqa: BLE001
                logger.error("Error en callback on_estado: %s", e)


# ---------------------------------------------------------------------------
# Subclase de baresipy (solo se define si la librería está disponible).
# Las firmas usan *args para tolerar diferencias de versión de baresipy.
# ---------------------------------------------------------------------------
if _BARESIP_OK:

    class _Telefono(BareSIP):
        """Teléfono SIP: mapea los eventos de baresip a on_estado del servicio."""

        def __init__(self, servicio: "SipService", user, pwd, gateway,
                     transport="udp", trunk=False):
            self._svc = servicio
            self._trunk = trunk
            # block=False: no bloquear; baresip corre en su propio hilo.
            super().__init__(user, pwd, gateway, transport=transport, block=False)

        # --- Listo (baresip arrancó) ---
        def handle_ready(self, *a):  # noqa: N802
            # En modo TRUNK no hay registro: baresipy solo marca ready al
            # registrar, así que lo forzamos aquí para poder marcar.
            if self._trunk:
                self.ready = True
                self._svc._emitir("registrado", "trunk (sin registro)")
            try:
                super().handle_ready()
            except Exception:  # noqa: BLE001
                pass

        # --- Registro ---
        def handle_login_success(self, *a):  # noqa: N802
            self._svc._emitir("registrado", "")

        def handle_login_failure(self, *a):  # noqa: N802
            if self._trunk:
                return  # en trunk el registro no aplica; ignorar
            self._svc._emitir("error_registro", str(a[0]) if a else "")

        # --- Llamada saliente ---
        def handle_call_ringing(self, *a):  # noqa: N802
            self._svc._emitir("timbrando", "")

        def handle_call_established(self, *a):  # noqa: N802
            self._svc._emitir("en_llamada", "")
            # Anuncio dentro de la llamada (ej. "tiene una llamada desde la
            # portería, de parte de X"). En un hilo aparte: send_audio duerme
            # mientras reproduce y no hay que bloquear el loop de eventos.
            wav = getattr(self._svc, "_anuncio_wav", None)
            if wav:
                self._svc._anuncio_wav = None
                import threading as _th
                _th.Thread(target=self._reproducir_anuncio, args=(wav,),
                           daemon=True).start()

        def _reproducir_anuncio(self, wav):
            import os
            import time as _t
            # El TTS se lanza cuando la IA toma el nombre, así que normalmente
            # ya está listo; margen corto por si acaso.
            for _ in range(10):
                if os.path.exists(wav):
                    break
                _t.sleep(0.4)
            if not os.path.exists(wav):
                logger.warning("Anuncio TTS no disponible; la llamada sigue sin anuncio.")
                return
            _t.sleep(0.8)   # dejar asentar el audio de la llamada (celular)
            try:
                self._svc._emitir("anunciando", os.path.basename(wav))
                self.send_audio(wav)   # reproduce y devuelve el mic (alsa,default)
                logger.info("Anuncio reproducido en la llamada; mic restaurado.")
            except Exception as e:  # noqa: BLE001
                logger.warning("No se pudo reproducir el anuncio: %s", e)

        def handle_call_ended(self, *a):  # noqa: N802
            self._svc._emitir("colgado", str(a[0]) if a else "")

        def handle_call_rejected(self, *a):  # noqa: N802
            self._svc._emitir("fallo_llamada", str(a[0]) if a else "")

        def handle_error(self, *a):  # noqa: N802
            self._svc._emitir("fallo_llamada", str(a[0]) if a else "")

        # --- Llamadas entrantes: el kiosco solo hace salientes; rechazar ---
        def handle_incoming_call(self, *a):  # noqa: N802
            try:
                self.hang()
            except Exception:  # noqa: BLE001
                pass
