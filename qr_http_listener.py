"""
qr_http_listener.py
-------------------
Recibe escaneos de un lector de QR de RED (ej. FondVision HZ2C3BAE) que, en modo
'Output format = HTTP', hace una petición HTTP por cada lectura:

    GET /qa/mcardsea.php?cardid=<CODIGO>&mjihao=1&cjihao=<idLector>&status=11&time=...

El código del QR viaja en el parámetro `cardid`. Este listener levanta un
servidor HTTP en un hilo de fondo y llama `on_scan(codigo)` por cada lectura,
igual que el ScannerListener del lector USB — así el flujo de retiro es el mismo.

Detalles del hardware:
  - El lector REPITE la misma lectura varias veces (3-4) por escaneo, así que se
    hace debounce: se ignora el mismo código si llega de nuevo dentro de unos
    segundos.
  - Responde 200 OK a toda petición para que el lector no reintente en bucle.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


class QRHttpListener:

    def __init__(self, on_scan, puerto: int = 8080, parametro: str = "cardid",
                 debounce_seg: float = 5.0):
        self.on_scan = on_scan
        self.puerto = puerto
        self.parametro = parametro
        self.debounce_seg = debounce_seg
        self._ultimo_codigo = ""
        self._ultimo_ts = 0.0
        self._srv: ThreadingHTTPServer | None = None
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> bool:
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def _responder(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")

            def do_GET(self):
                try:
                    listener._procesar(self.path)
                finally:
                    self._responder()

            # Algunos firmwares envían POST; se trata igual.
            do_POST = do_GET

            def log_message(self, *a):
                pass  # sin ruido en stderr

        try:
            self._srv = ThreadingHTTPServer(("0.0.0.0", self.puerto), _Handler)
        except OSError as e:
            logger.error("Lector QR de red: no se pudo abrir el puerto %s: %s",
                         self.puerto, e)
            return False

        self._hilo = threading.Thread(target=self._srv.serve_forever,
                                      name="QRHttpListener", daemon=True)
        self._hilo.start()
        logger.info("Lector QR de red ACTIVO en :%s (param=%s).",
                    self.puerto, self.parametro)
        return True

    def _procesar(self, path: str):
        try:
            query = parse_qs(urlparse(path).query)
            codigo = (query.get(self.parametro) or [""])[0].strip()
            if not codigo:
                return
            ahora = time.time()
            # Debounce: el lector repite la misma lectura varias veces.
            if (codigo == self._ultimo_codigo
                    and (ahora - self._ultimo_ts) < self.debounce_seg):
                return
            self._ultimo_codigo = codigo
            self._ultimo_ts = ahora
            logger.info("Lector QR de red: código recibido.")
            self.on_scan(codigo)
        except Exception as e:  # noqa: BLE001 - el hilo del server nunca debe morir
            logger.error("Lector QR de red: error procesando la lectura: %s", e)

    def detener(self):
        if self._srv is not None:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("Lector QR de red detenido.")
