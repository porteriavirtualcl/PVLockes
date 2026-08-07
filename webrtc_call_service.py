"""
webrtc_call_service.py
----------------------
Llamada de AUDIO bidireccional del kiosco a la app WEB del residente, por WebRTC.

Flujo (el kiosco es quien LLAMA):
  1. Crea una PeerConnection (aiortc) con el micrófono del speakerphone.
  2. Genera la oferta SDP y espera a completar el gathering de ICE
     (no-trickle: la SDP ya incluye los candidatos → señalización simple).
  3. Crea el doc `calls/{id}` en Firestore con la oferta (status=ringing).
  4. Sondea el doc hasta que la app del residente escriba la respuesta (answerSdp).
  5. Aplica la respuesta → se establece el audio bidireccional.
  6. Sigue sondeando: si status pasa a 'ended'/'rejected', o el usuario cuelga,
     cierra la llamada.

Audio: se usa **sounddevice** (PortAudio) para capturar del micrófono y
reproducir en el parlante, porque el PyAV de piwheels no trae los dispositivos
alsa/pulse. El ALSA `default` de la Pi está ruteado al speakerphone USB.

TOLERANTE A FALLOS: si aiortc/sounddevice no están o falta Firebase, el servicio
queda inactivo y el kiosco funciona igual. Corre en su propio hilo (asyncio);
los cambios de estado se informan por `on_estado(estado, detalle)`, que la GUI
debe reprogramar con `after()`.
"""

from __future__ import annotations

import uuid
import fractions
import logging
import threading
import asyncio
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    import numpy as np
    import sounddevice as sd
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
    from aiortc import (RTCPeerConnection, RTCConfiguration, RTCIceServer,
                        RTCSessionDescription, MediaStreamTrack)
    _WEBRTC_OK = True
except Exception as _e:  # noqa: BLE001
    _WEBRTC_OK = False
    logger.info("WebRTC no disponible (%s); la llamada al residente quedará inactiva.", _e)

# Audio: 48 kHz mono, tramas de 20 ms (lo que espera Opus).
SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLES_20MS = SAMPLE_RATE * 20 // 1000  # 960


if _WEBRTC_OK:

    # Servidores ICE: STUN sirve en la MISMA red; TURN es necesario para
    # residentes en OTRA red (celular/casa) porque el NAT bloquea el P2P.
    # (TURN público de prueba Open Relay; para producción, un TURN dedicado.)
    ICE_SERVERS = [
        RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        RTCIceServer(urls=["turn:2.24.85.59:3478"],
                     username="porteria", credential="PvTurn2026Kx9r"),
        RTCIceServer(urls=["turn:2.24.85.59:3478?transport=tcp"],
                     username="porteria", credential="PvTurn2026Kx9r"),
    ]

    class _MicTrack(MediaStreamTrack):
        """Pista de audio del micrófono (speakerphone) vía sounddevice."""
        kind = "audio"

        def __init__(self, loop):
            super().__init__()
            self._loop = loop
            self._q: "asyncio.Queue" = asyncio.Queue(maxsize=25)
            self._pts = 0

            def _push(data):  # corre en el loop
                if self._q.full():
                    try:
                        self._q.get_nowait()  # descarta la trama más vieja (baja latencia)
                    except asyncio.QueueEmpty:
                        pass
                try:
                    self._q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

            def _cb(indata, frames, time_info, status):  # hilo de PortAudio
                try:
                    self._loop.call_soon_threadsafe(_push, bytes(indata))
                except Exception:  # noqa: BLE001
                    pass

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=SAMPLES_20MS, callback=_cb)
            self._stream.start()

        async def recv(self):
            data = await self._q.get()
            samples = np.frombuffer(data, dtype=np.int16).reshape(1, -1)  # (1, N) s16 mono
            frame = AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
            self._pts += samples.shape[1]
            return frame

        def stop(self):
            super().stop()
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass


class WebRTCCallService:
    """Servicio de llamada de audio kiosco → app del residente."""

    def __init__(self, firebase, config_mgr, on_estado=None):
        self.firebase = firebase
        self.cfg = config_mgr
        self.on_estado = on_estado
        self.disponible = _WEBRTC_OK and firebase is not None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pc = None
        self._mic = None
        self._call_id: str | None = None
        self._cancel = False

    # ------------------------------------------------------------------ #
    # API pública (desde el hilo de la GUI)
    # ------------------------------------------------------------------ #
    def llamar(self, residente: dict):
        """Inicia la llamada. `residente`: {'uid': ..., 'nombre': ...}."""
        if not self.disponible:
            self._emit("no_disponible", "WebRTC no disponible")
            return
        if self._thread and self._thread.is_alive():
            return
        self._cancel = False
        self._thread = threading.Thread(
            target=self._run, args=(residente,), name="webrtc-call", daemon=True)
        self._thread.start()

    def colgar(self):
        self._cancel = True
        loop, pc = self._loop, self._pc
        if loop is not None and pc is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._hangup(), loop)
            except Exception:  # noqa: BLE001
                pass

    def detener(self):
        self.colgar()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4)

    # ------------------------------------------------------------------ #
    # Hilo + asyncio
    # ------------------------------------------------------------------ #
    def _run(self, residente):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._llamada(residente))
        except Exception as e:  # noqa: BLE001
            logger.error("Error en la llamada WebRTC: %s", e)
            self._emit("fallo", str(e))
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    async def _llamada(self, residente):
        self._emit("preparando", residente.get("nombre", ""))
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ICE_SERVERS))
        self._pc = pc
        self._mic = _MicTrack(self._loop)
        pc.addTrack(self._mic)

        @pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                self._loop.create_task(self._reproducir(track))

        @pc.on("connectionstatechange")
        async def _on_state():
            logger.info("WebRTC connectionState=%s", pc.connectionState)
            if pc.connectionState == "connected":
                self._emit("en_llamada", "")
            elif pc.connectionState in ("failed", "closed", "disconnected"):
                self._emit("colgado", pc.connectionState)

        # Oferta + esperar candidatos ICE (no-trickle).
        await pc.setLocalDescription(await pc.createOffer())
        await self._esperar_ice(pc)

        # Publicar la llamada en Firestore.
        self._call_id = uuid.uuid4().hex[:20]
        ahora = datetime.now(timezone.utc).isoformat()
        await self._ejec(self.firebase.crear_llamada, self._call_id, {
            "from": self.cfg.kiosk_id,
            "from_name": f"Portería · {self.cfg.condo_name}",
            "to": residente.get("uid", ""),
            "to_name": residente.get("nombre", ""),
            "condo_id": self.cfg.condo_id,
            "status": "ringing",
            "offer_type": pc.localDescription.type,
            "offer_sdp": pc.localDescription.sdp,
            "created_at": ahora,
        })
        self._emit("timbrando", residente.get("nombre", ""))

        # Sondear la respuesta del residente (hasta ~60 s).
        answer = None
        for _ in range(60):
            if self._cancel:
                break
            await asyncio.sleep(1)
            doc = await self._ejec(self.firebase.obtener_llamada, self._call_id)
            if not doc:
                continue
            if doc.get("status") in ("ended", "rejected"):
                self._emit("rechazada", "")
                await self._hangup()
                return
            if doc.get("answerSdp"):
                answer = RTCSessionDescription(
                    sdp=doc["answerSdp"], type=doc.get("answerType", "answer"))
                break

        if answer is None:
            if not self._cancel:
                self._emit("sin_respuesta", "")
            await self._hangup()
            return

        await pc.setRemoteDescription(answer)

        # En llamada: seguir hasta que alguien cuelgue.
        while not self._cancel:
            await asyncio.sleep(1)
            doc = await self._ejec(self.firebase.obtener_llamada, self._call_id)
            if doc and doc.get("status") in ("ended", "rejected"):
                self._emit("colgado", "remoto")
                break
            if pc.connectionState in ("failed", "closed"):
                break
        await self._hangup()

    async def _reproducir(self, track):
        """Reproduce el audio remoto en el parlante (sounddevice)."""
        try:
            resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                     dtype="int16", blocksize=SAMPLES_20MS)
            stream.start()
        except Exception as e:  # noqa: BLE001
            logger.error("No se pudo abrir el parlante: %s", e)
            return
        try:
            while True:
                frame = await track.recv()
                for rf in resampler.resample(frame):
                    arr = rf.to_ndarray().reshape(-1).astype(np.int16)
                    stream.write(arr)
        except Exception:  # noqa: BLE001  (track terminó)
            pass
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    async def _esperar_ice(self, pc):
        if pc.iceGatheringState == "complete":
            return
        fut = self._loop.create_future()

        @pc.on("icegatheringstatechange")
        def _c():
            if pc.iceGatheringState == "complete" and not fut.done():
                fut.set_result(None)

        try:
            await asyncio.wait_for(fut, timeout=8)
        except asyncio.TimeoutError:
            logger.warning("ICE gathering no completó en 8s; se envía lo reunido.")

    async def _hangup(self):
        try:
            if self._call_id:
                await self._ejec(self.firebase.actualizar_llamada, self._call_id,
                                 {"status": "ended", "endedBy": "kiosk"})
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._mic:
                self._mic.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pc:
                await self._pc.close()
        except Exception:  # noqa: BLE001
            pass
        self._pc = None
        self._mic = None
        self._emit("finalizado", "")

    async def _ejec(self, fn, *args):
        """Ejecuta una función bloqueante (requests) fuera del loop asyncio."""
        return await self._loop.run_in_executor(None, fn, *args)

    def _emit(self, estado: str, detalle: str = ""):
        logger.info("WebRTC estado=%s %s", estado, f"({detalle})" if detalle else "")
        if self.on_estado:
            try:
                self.on_estado(estado, detalle)
            except Exception as e:  # noqa: BLE001
                logger.error("Error en callback on_estado: %s", e)
