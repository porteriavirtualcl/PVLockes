"""
ai_assistant_service.py
-----------------------
Conserje IA de voz (piloto) para el kiosco "Portería Virtual".

Objetivo del piloto: cuando un repartidor va a DEJAR una encomienda, un agente
de voz lo atiende, le pregunta su NOMBRE y su EMPRESA de reparto, le resuelve
dudas de cómo funciona el locker y, cuando corresponde, ABRE un casillero
llamando a la herramienta `abrir_casillero` (function calling).

Motor: Gemini Live API (audio en tiempo real: STT + LLM + TTS + turnos en una
sola conexión WebSocket). Se eligió esta vía para NO depender de Retell:
reutiliza el pipeline de audio `sounddevice` que ya usa el kiosco y las
credenciales de Google que ya se pagan en la app de producción.

Audio (según la Live API):
  - ENTRADA (micrófono): PCM 16-bit, 16 kHz, mono, little-endian.
  - SALIDA (parlante):   PCM 16-bit, 24 kHz, mono.
El ALSA `default` de la Pi ya está ruteado al speakerphone USB (mic + parlante
con cancelación de eco).

TOLERANTE A FALLOS: si `google-genai`/`sounddevice` no están instalados o falta
la API key, el servicio queda inactivo (`disponible == False`) y el kiosco
funciona igual, sin el asistente.

Corre en su propio hilo (asyncio). Los cambios de estado y las transcripciones
se informan por `on_estado(tipo, texto)`, que la GUI debe reprogramar con
`after()` (Tkinter no es thread-safe). La apertura del casillero se delega a
`on_abrir_casillero(datos) -> dict`, que el kiosco implementa con el
ResourceAllocator + HardwareManager + LocalStore reales.

Prueba rápida (sin kiosco, con la apertura MOCKEADA):
    export GEMINI_API_KEY="tu_api_key"      # o ponla en config.json -> asistente_ia.api_key
    python ai_assistant_service.py
Habla por el micrófono: el asistente debería saludarte, pedir nombre + empresa
y, al confirmar, "abrir" un casillero de prueba (impreso por consola).
"""

from __future__ import annotations

import os
import json
import queue
import logging
import threading
import asyncio

logger = logging.getLogger(__name__)

try:
    import numpy as np  # noqa: F401  (se usa para chequear/segurizar tipos)
    import sounddevice as sd
    from google import genai
    from google.genai import types
    _AI_OK = True
except Exception as _e:  # noqa: BLE001
    _AI_OK = False
    logger.info("Asistente IA no disponible (%s); el kiosco funcionará sin él.", _e)

# --- Parámetros de audio exigidos por la Live API ---
SR_IN = 16000          # micrófono -> Gemini
SR_OUT = 24000         # Gemini -> parlante
CHANNELS = 1
BLOCK_IN = 640         # 40 ms @ 16 kHz (bloques cómodos para streaming)

# Modelo Live por defecto. 'native-audio' = voz más natural y detección de
# idioma por contexto (habla español porque el system prompt lo indica).
# Configurable en config.json (asistente_ia.modelo).
MODELO_DEFECTO = "gemini-2.5-flash-native-audio-latest"

# Instrucción de sistema: define el rol y el guion del conserje.
SYSTEM_PROMPT = (
    "Eres el asistente de voz de 'Portería Virtual', un conserje virtual amable "
    "de un condominio en Chile. Estás atendiendo a un REPARTIDOR que llega a "
    "DEJAR una encomienda en el locker. Habla en español chileno, con frases "
    "cortas, claras y cordiales.\n\n"
    "Tu guion:\n"
    "1) Saluda breve y pregunta, de a poco: el NOMBRE del repartidor, la EMPRESA "
    "de reparto (Blue Express, Correos de Chile, DHL, Falabella, Mercado Libre, "
    "Uber, u otra) y el NÚMERO de departamento o casa de destino.\n"
    "2) Si el repartidor tiene dudas de cómo funciona el locker, explícale simple: "
    "se abrirá un casillero automáticamente, debe dejar el paquete adentro y CERRAR "
    "bien la puerta; el residente recibirá un aviso y lo retirará después con un "
    "código QR. Un solo paquete por casillero.\n"
    "3) Cuando ya tengas NOMBRE, EMPRESA y NÚMERO de unidad, usa la herramienta "
    "'abrir_casillero' para abrir un casillero disponible. No inventes números de "
    "casillero: usa el que te devuelva la herramienta.\n"
    "4) Cuando la herramienta confirme el casillero, dile al repartidor el número, "
    "que deje el paquete y cierre la puerta, y despídete brevemente en una frase.\n"
    "5) Si la herramienta indica que NO hay casilleros disponibles, discúlpate y "
    "pídele que use el timbre para llamar al residente. No inventes información.\n\n"
    "Reglas de conversación (IMPORTANTES, es un kiosco en la calle con ruido):\n"
    "- Pregunta cada dato UNA sola vez. Si no se entendió, pide repetir UNA vez "
    "más y ACEPTA lo que escuches (no pidas apellido ni deletreo).\n"
    "- NUNCA hagas la misma pregunta más de dos veces; continúa con lo que tengas.\n"
    "- Ignora ruidos o frases sueltas sin sentido: no respondas a eso.\n"
    "- Frases de máximo 12 palabras."
)


# Recepcionista de VOZ del botón "Llamar": deriva según lo que necesite la
# persona en el acceso (hablar con el residente / dejar encomienda / operador).
SYSTEM_PROMPT_RECEPCION = (
    "Eres la voz de recepción de 'Portería Virtual', el conserje virtual de un "
    "condominio en Chile. Una persona presionó el botón LLAMAR en el kiosco de "
    "acceso. Habla en español chileno, cordial y con frases MUY cortas.\n\n"
    "Tu guion:\n"
    "1) Saluda: 'Portería Virtual, ¿en qué le puedo ayudar?' y escucha.\n"
    "2) Según lo que necesite:\n"
    "   a) HABLAR CON EL RESIDENTE (visita, delivery, consulta a la persona que "
    "vive ahí): PRIMERO pregunta su NOMBRE ('¿Me indica su nombre, por favor?'). "
    "Cuando lo tengas, dile 'Un momento, le comunico con el residente' y usa la "
    "herramienta 'comunicar_residente' con el nombre. Después despídete en una "
    "frase.\n"
    "   b) DEJAR UNA ENCOMIENDA: explícale simple: debe tocar el botón 'Dejar "
    "Encomienda' en la pantalla (o 'Dejar con Asistente'), indicar el "
    "departamento y la empresa; se abrirá un casillero automáticamente, deja el "
    "paquete y CIERRA bien la puerta; el residente recibe un aviso con código QR "
    "para retirarla. Pregunta si necesita algo más.\n"
    "   c) AYUDA CON EL SISTEMA o cualquier problema que no puedas resolver: "
    "dile 'Le comunico con un operador de Portería Virtual' y usa la herramienta "
    "'llamar_operador'. Después despídete en una frase.\n"
    "3) No inventes información. No des datos de residentes ni del condominio.\n\n"
    "Reglas de conversación (IMPORTANTES, es un kiosco en la calle con ruido):\n"
    "- Pregunta cada cosa UNA sola vez. Si no se entendió, pide repetir UNA vez "
    "más y ACEPTA lo que escuches: basta el primer nombre, no pidas apellido, "
    "no deletrees, no confirmes de nuevo.\n"
    "- NUNCA hagas la misma pregunta más de dos veces; continúa con lo que tengas.\n"
    "- Ignora ruidos, música o frases sueltas sin sentido: no respondas a eso; "
    "sigue esperando o retoma tu última pregunta sin repetirla completa.\n"
    "- Frases de máximo 12 palabras."
)


def _declaraciones_recepcion():
    """Herramientas del recepcionista: transferir a residente u operador."""
    return [
        types.FunctionDeclaration(
            name="comunicar_residente",
            description=("Transfiere la llamada al RESIDENTE del condominio. Usar "
                         "cuando la persona necesita hablar con quien vive ahí, "
                         "DESPUÉS de preguntarle su nombre."),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "nombre_visitante": types.Schema(
                        type=types.Type.STRING,
                        description="Nombre de la persona que está en el acceso."),
                    "motivo": types.Schema(
                        type=types.Type.STRING,
                        description="Motivo breve (ej: visita, delivery). Opcional."),
                },
                required=["nombre_visitante"],
            ),
        ),
        types.FunctionDeclaration(
            name="llamar_operador",
            description=("Transfiere la llamada a un OPERADOR humano de Portería "
                         "Virtual. Usar cuando necesita ayuda con el sistema o "
                         "algo que el asistente no puede resolver."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
    ]


def _declaracion_herramienta():
    """FunctionDeclaration de `abrir_casillero` para el modelo."""
    return types.FunctionDeclaration(
        name="abrir_casillero",
        description=(
            "Asigna y abre un casillero disponible del locker para que el "
            "repartidor deje la encomienda. Llamar SOLO cuando ya se tiene el "
            "nombre del repartidor y la empresa."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "nombre_repartidor": types.Schema(
                    type=types.Type.STRING,
                    description="Nombre del repartidor que deja la encomienda.",
                ),
                "empresa": types.Schema(
                    type=types.Type.STRING,
                    description="Empresa de reparto (courier).",
                ),
                "unidad": types.Schema(
                    type=types.Type.STRING,
                    description="N° de depto/casa destinataria, si el repartidor lo indica. Opcional.",
                ),
            },
            required=["nombre_repartidor", "empresa"],
        ),
    )


class AIAssistantService:
    """Conserje IA de voz para el flujo de dejar encomienda (piloto)."""

    def __init__(self, api_key: str, on_estado=None, on_abrir_casillero=None,
                 modelo: str = MODELO_DEFECTO, idioma: str = "es-US",
                 modo: str = "encomienda", on_accion=None):
        """
        Args:
            api_key: API key de Gemini (Google AI Studio).
            on_estado(tipo, texto): callback de estado/transcripción para la GUI.
                tipos: 'conectando' | 'en_conversacion' | 'ia_habla' | 'usuario_habla'
                       | 'transcripcion_ia' | 'transcripcion_usuario'
                       | 'abriendo_casillero' | 'casillero_abierto'
                       | 'finalizado' | 'error'
            on_abrir_casillero(datos)->dict: ejecuta la apertura real. Recibe
                {'nombre_repartidor','empresa','unidad'} y devuelve, p.ej.:
                {'ok': True, 'casillero': 'L3', 'mensaje': '...'} o
                {'ok': False, 'mensaje': 'No hay casilleros disponibles.'}
        """
        self.api_key = api_key
        self.modelo = modelo or MODELO_DEFECTO
        self.idioma = idioma or "es-US"
        self.on_estado = on_estado
        self.on_abrir_casillero = on_abrir_casillero
        # modo: 'encomienda' (ayuda al repartidor y abre casillero) o
        #       'recepcion' (contesta el botón Llamar y deriva/transfiere).
        self.modo = modo
        # on_accion(nombre, args) -> dict: acciones del modo recepción
        # (comunicar_residente / llamar_operador). Corre en el hilo del asistente.
        self.on_accion = on_accion

        self.disponible = _AI_OK and bool(api_key)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._session = None
        self._cancel = False

        self._in_stream = None
        self._out_stream = None
        # Buffer de reproducción (bytes s16 @ 24 kHz) protegido por lock; el
        # callback de PortAudio lo consume. En 'interrupted' se vacía (barge-in).
        self._play_buf = bytearray()
        self._play_lock = threading.Lock()
        self._mic_q: "asyncio.Queue | None" = None

    # ------------------------------------------------------------------ #
    # API pública (desde el hilo de la GUI)
    # ------------------------------------------------------------------ #
    def iniciar(self):
        """Arranca la conversación en un hilo propio."""
        if not self.disponible:
            self._emit("error", "Asistente IA no disponible (falta google-genai/sounddevice o API key).")
            return
        if self._thread and self._thread.is_alive():
            return
        self._cancel = False
        self._thread = threading.Thread(target=self._run, name="ai-assistant", daemon=True)
        self._thread.start()

    def terminar(self):
        """Solicita cerrar la conversación (corta aunque esté esperando audio)."""
        self._cancel = True
        loop, task = self._loop, self._main_task
        if loop is not None and task is not None:
            # Cancela la tarea principal en su propio loop -> desbloquea receive()
            # y cierra la sesión limpiamente por el context manager.
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:  # noqa: BLE001
                pass

    def detener(self):
        self.terminar()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ #
    # Hilo + asyncio
    # ------------------------------------------------------------------ #
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._conversar())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass  # cierre solicitado (terminar / auto-fin)
        except Exception as e:  # noqa: BLE001
            logger.error("Error en la conversación IA: %s", e)
            self._emit("error", str(e))
        finally:
            self._main_task = None
            self._cerrar_audio()
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None
            self._emit("finalizado", "")

    async def _conversar(self):
        self._emit("conectando", "")
        client = genai.Client(api_key=self.api_key)

        if self.modo == "recepcion":
            prompt = SYSTEM_PROMPT_RECEPCION
            declaraciones = _declaraciones_recepcion()
            nudge = ("(Una persona acaba de presionar el botón Llamar en el "
                     "kiosco. Salúdala y pregunta en qué puedes ayudar.)")
        else:
            prompt = SYSTEM_PROMPT
            declaraciones = [_declaracion_herramienta()]
            nudge = ("(El repartidor acaba de acercarse al kiosco para dejar una "
                     "encomienda. Salúdalo y comienza tu guion.)")
        self._nudge = nudge

        cfg_kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(parts=[types.Part(text=prompt)]),
            tools=[types.Tool(function_declarations=declaraciones)],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        # Los modelos 'native-audio' detectan el idioma por contexto; forzar
        # language_code puede dar error. Solo se fija en modelos NO native-audio.
        if "native-audio" not in self.modelo and self.idioma:
            cfg_kwargs["speech_config"] = types.SpeechConfig(language_code=self.idioma)

        # Detección de voz (VAD) afinada para KIOSCO EN LA CALLE: menos sensible
        # al inicio (ignora ruido ambiente) y con más silencio antes de dar por
        # terminado el turno (no corta el nombre a la mitad -> evita re-preguntar).
        try:
            cfg_kwargs["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=200,
                    silence_duration_ms=900,
                ))
        except Exception as e:  # noqa: BLE001  (por si cambia la API del SDK)
            logger.info("VAD custom no disponible en este SDK (%s); se usa el default.", e)

        config = types.LiveConnectConfig(**cfg_kwargs)

        self._mic_q = asyncio.Queue(maxsize=50)
        self._abrir_audio()

        # Seguridad: nunca dejar la sesión abierta más de ~3 min (por si el
        # repartidor se va sin cerrar, evita gastar cuota indefinidamente).
        self._loop.call_later(180, self._cancelar_por_fin)

        # La Live API (modelos preview) puede cortar con error 1011 (interno del
        # servidor) de forma intermitente. Reconectamos hasta unas pocas veces
        # para que una caída transitoria no termine la atención al repartidor.
        MAX_INTENTOS = 4
        intentos = 0
        while not self._cancel and intentos < MAX_INTENTOS:
            intentos += 1
            try:
                async with client.aio.live.connect(model=self.modelo, config=config) as session:
                    self._session = session
                    self._emit("en_conversacion", "")
                    # Empuja al modelo a saludar primero (sin esperar al repartidor).
                    try:
                        await session.send_client_content(
                            turns=types.Content(role="user",
                                                parts=[types.Part(text=self._nudge)]),
                            turn_complete=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.debug("No se pudo enviar el saludo inicial: %s", e)

                    tarea_mic = asyncio.create_task(self._enviar_microfono())
                    try:
                        await self._recibir(session)
                    finally:
                        tarea_mic.cancel()
                        await asyncio.gather(tarea_mic, return_exceptions=True)
                        self._session = None
                # _recibir retornó sin excepción: la sesión terminó normalmente.
                break
            except Exception as e:  # noqa: BLE001
                if self._cancel:
                    break
                logger.warning("Sesión Live cayó (%s); reconectando (intento %s/%s)...",
                               str(e)[:90], intentos, MAX_INTENTOS)
                self._emit("reconectando", str(e)[:60])
                with self._play_lock:
                    self._play_buf.clear()
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ #
    # Envío del micrófono -> Gemini
    # ------------------------------------------------------------------ #
    async def _enviar_microfono(self):
        try:
            while not self._cancel:
                data = await self._mic_q.get()
                if data is None:
                    break
                try:
                    await self._session.send_realtime_input(
                        audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={SR_IN}"))
                except Exception as e:  # noqa: BLE001
                    logger.debug("Error enviando audio: %s", e)
                    break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Recepción de Gemini -> parlante + tool calls + transcripciones
    # ------------------------------------------------------------------ #
    async def _recibir(self, session):
        # En google-genai, session.receive() entrega los mensajes de UN turno y
        # el async-for termina al cerrarse ese turno. Para una conversación
        # continua hay que volver a llamarlo en cada turno.
        while not self._cancel:
            recibio = False
            async for msg in session.receive():
                recibio = True
                if self._cancel:
                    return

                # 1) Audio del asistente -> buffer de reproducción.
                data = getattr(msg, "data", None)
                if data:
                    with self._play_lock:
                        self._play_buf.extend(data)
                    self._emit("ia_habla", "")

                # 2) Contenido del servidor: transcripciones, interrupción, fin de turno.
                sc = getattr(msg, "server_content", None)
                if sc is not None:
                    it = getattr(sc, "input_transcription", None)
                    if it and getattr(it, "text", None):
                        self._emit("transcripcion_usuario", it.text)
                    ot = getattr(sc, "output_transcription", None)
                    if ot and getattr(ot, "text", None):
                        self._emit("transcripcion_ia", ot.text)
                    if getattr(sc, "interrupted", False):
                        # Barge-in: el repartidor habló encima -> descartar audio en cola.
                        with self._play_lock:
                            self._play_buf.clear()

                # 3) Tool calls (abrir_casillero).
                tc = getattr(msg, "tool_call", None)
                if tc is not None:
                    await self._atender_tool_calls(session, tc)

            if not recibio:
                # Turno vacío: la sesión se cerró del lado del servidor.
                return

    async def _atender_tool_calls(self, session, tool_call):
        respuestas = []
        for fc in getattr(tool_call, "function_calls", []) or []:
            if fc.name == "abrir_casillero":
                args = dict(fc.args or {})
                self._emit("abriendo_casillero",
                           f"{args.get('nombre_repartidor','')} · {args.get('empresa','')}")
                # La apertura real puede bloquear (GPIO ~1.5 s): fuera del loop.
                resultado = await self._loop.run_in_executor(
                    None, self._ejecutar_apertura, args)
                if resultado.get("ok"):
                    self._emit("casillero_abierto", resultado.get("casillero", ""))
                    # Deja que el asistente dé el mensaje final y cierra la sesión
                    # (evita que quede "escuchando" ruido ambiente tras la entrega).
                    self._loop.call_later(12, self._cancelar_por_fin)
                respuestas.append(types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name, response=resultado))
            elif self.on_accion is not None:
                # Acciones del modo recepción (comunicar_residente / llamar_operador).
                args = dict(fc.args or {})
                self._emit("transfiriendo", fc.name)
                try:
                    resultado = await self._loop.run_in_executor(
                        None, self.on_accion, fc.name, args)
                    if not isinstance(resultado, dict):
                        resultado = {"ok": True}
                except Exception as e:  # noqa: BLE001
                    logger.error("Error en on_accion(%s): %s", fc.name, e)
                    resultado = {"ok": False, "mensaje": "No se pudo completar la acción."}
                respuestas.append(types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name, response=resultado))
                if resultado.get("ok"):
                    # Deja que la IA se despida y cierra para liberar el audio
                    # antes de marcar por SIP (la GUI transfiere al 'finalizado').
                    self._loop.call_later(7, self._cancelar_por_fin)
            else:
                respuestas.append(types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name,
                    response={"ok": False, "mensaje": "Herramienta desconocida."}))
        if respuestas:
            try:
                await session.send_tool_response(function_responses=respuestas)
            except Exception as e:  # noqa: BLE001
                logger.error("Error enviando tool_response: %s", e)

    def _ejecutar_apertura(self, args: dict) -> dict:
        """Delega en el callback del kiosco; si no hay, mockea (piloto suelto)."""
        if self.on_abrir_casillero is not None:
            try:
                r = self.on_abrir_casillero(args)
                return r if isinstance(r, dict) else {"ok": True, "casillero": str(r)}
            except Exception as e:  # noqa: BLE001
                logger.error("Error en on_abrir_casillero: %s", e)
                return {"ok": False, "mensaje": "Ocurrió un problema al abrir el casillero."}
        # Sin callback: modo prueba.
        logger.info("[MOCK abrir_casillero] %s", args)
        return {"ok": True, "casillero": "L3",
                "mensaje": "Casillero L3 abierto (simulado)."}

    # ------------------------------------------------------------------ #
    # Audio: PortAudio (sounddevice)
    # ------------------------------------------------------------------ #
    def _abrir_audio(self):
        # Entrada: micrófono -> cola asyncio (thread-safe vía call_soon_threadsafe).
        def _in_cb(indata, frames, time_info, status):  # hilo de PortAudio
            if self._cancel:
                return
            try:
                self._loop.call_soon_threadsafe(self._encolar_mic, bytes(indata))
            except Exception:  # noqa: BLE001
                pass

        self._in_stream = sd.RawInputStream(
            samplerate=SR_IN, channels=CHANNELS, dtype="int16",
            blocksize=BLOCK_IN, callback=_in_cb)
        self._in_stream.start()

        # Salida: callback que consume el buffer de reproducción.
        def _out_cb(outdata, frames, time_info, status):  # hilo de PortAudio
            need = frames * 2  # int16 mono
            with self._play_lock:
                avail = len(self._play_buf)
                take = min(need, avail)
                if take:
                    outdata[:take] = bytes(self._play_buf[:take])
                    del self._play_buf[:take]
                if take < need:
                    outdata[take:need] = b"\x00" * (need - take)

        self._out_stream = sd.RawOutputStream(
            samplerate=SR_OUT, channels=CHANNELS, dtype="int16", callback=_out_cb)
        self._out_stream.start()

    def _encolar_mic(self, data: bytes):
        if self._mic_q is None:
            return
        if self._mic_q.full():
            try:
                self._mic_q.get_nowait()   # descarta lo más viejo (baja latencia)
            except asyncio.QueueEmpty:
                pass
        try:
            self._mic_q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _cerrar_audio(self):
        for s in (self._in_stream, self._out_stream):
            try:
                if s is not None:
                    s.stop()
                    s.close()
            except Exception:  # noqa: BLE001
                pass
        self._in_stream = None
        self._out_stream = None
        with self._play_lock:
            self._play_buf.clear()

    def _cancelar_por_fin(self):
        """Cierra la conversación (auto-fin tras abrir el casillero o por timeout)."""
        self._cancel = True
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()

    # ------------------------------------------------------------------ #
    def _emit(self, tipo: str, texto: str = ""):
        if tipo not in ("ia_habla",):  # 'ia_habla' es muy frecuente; no ensuciar el log
            logger.info("IA estado=%s %s", tipo, f"({texto})" if texto else "")
        if self.on_estado:
            try:
                self.on_estado(tipo, texto)
            except Exception as e:  # noqa: BLE001
                logger.error("Error en callback on_estado: %s", e)


def generar_anuncio_tts(api_key: str, texto: str, ruta_wav: str,
                        voz: str = "Kore",
                        modelo_tts: str = "gemini-2.5-flash-preview-tts") -> bool:
    """
    Genera un WAV (24 kHz mono s16) con Gemini TTS. Se usa para el ANUNCIO que
    se reproduce dentro de la llamada SIP al residente ("tiene una llamada desde
    la portería, de parte de ..."). Bloqueante (~1-3 s): llamar desde un hilo.

    Returns True si el archivo quedó escrito.
    """
    if not _AI_OK or not api_key:
        return False
    import wave
    try:
        client = genai.Client(api_key=api_key)
        r = client.models.generate_content(
            model=modelo_tts,
            contents=texto,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voz))),
            ),
        )
        pcm = r.candidates[0].content.parts[0].inline_data.data
        w = wave.open(ruta_wav, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
        w.close()
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("TTS del anuncio falló: %s", e)
        return False


# ---------------------------------------------------------------------------
# Prueba manual suelta (sin kiosco): python ai_assistant_service.py
# La apertura del casillero se MOCKEA (se imprime por consola).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    if not _AI_OK:
        raise SystemExit(
            "Faltan dependencias. Instala:  pip install google-genai sounddevice numpy")

    # API key: variable de entorno o config.json -> asistente_ia.api_key
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    modelo, idioma = MODELO_DEFECTO, "es-US"
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            _ai = json.load(f).get("asistente_ia", {})
            api_key = api_key or _ai.get("api_key", "")
            modelo = _ai.get("modelo", modelo)
            idioma = _ai.get("idioma", idioma)
    except FileNotFoundError:
        pass

    if not api_key:
        raise SystemExit(
            "Falta la API key. Exporta GEMINI_API_KEY o ponla en config.json (asistente_ia.api_key).")

    def _estado(tipo, texto):
        etiquetas = {
            "transcripcion_usuario": "🗣️  Repartidor",
            "transcripcion_ia": "🤖 Asistente",
            "abriendo_casillero": "🔓 Abriendo casillero para",
            "casillero_abierto": "✅ Casillero abierto",
        }
        if tipo in etiquetas:
            print(f"{etiquetas[tipo]}: {texto}")
        elif tipo in ("conectando", "en_conversacion", "finalizado", "error"):
            print(f"[{tipo}] {texto}")

    def _abrir(datos):
        print(f"\n>>> [MOCK] abrir_casillero({datos})\n")
        return {"ok": True, "casillero": "L3",
                "mensaje": "Casillero L3 abierto. Deja el paquete y cierra la puerta."}

    svc = AIAssistantService(api_key=api_key, on_estado=_estado,
                             on_abrir_casillero=_abrir, modelo=modelo, idioma=idioma)
    print("Conectando al asistente IA... habla por el micrófono. Ctrl+C para salir.\n")
    svc.iniciar()
    try:
        while svc._thread and svc._thread.is_alive():
            svc._thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nCerrando...")
        svc.detener()
