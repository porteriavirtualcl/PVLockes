"""
notification_service.py
------------------------
Genera el código/QR de retiro y "envía" el email al residente.

En esta versión el envío está SIMULADO: genera la imagen QR (si la librería
está disponible) y registra por consola el correo que se enviaría. El envío
real por SMTP queda esbozado en `_enviar_smtp` para activarlo más adelante.
"""

from __future__ import annotations

import os
import uuid
import logging

logger = logging.getLogger(__name__)


class NotificationService:

    def __init__(self, carpeta_qr: str = "qr_generados", simulado: bool = True):
        self.simulado = simulado
        self.carpeta_qr = carpeta_qr
        os.makedirs(self.carpeta_qr, exist_ok=True)

    # ------------------------------------------------------------------ #
    def generar_codigo_retiro(self) -> str:
        """
        Genera un código numérico de 6 dígitos para el retiro (compatible con
        el teclado táctil numérico y usado también en el QR).

        Nota: 6 dígitos dan 1.000.000 de combinaciones. Con bajo volumen de
        encomiendas pendientes la colisión es muy improbable, pero conviene
        verificar unicidad contra las entregas 'depositada' en producción.
        """
        # uuid4().int es un entero de 128 bits; tomamos 6 dígitos.
        return f"{uuid.uuid4().int % 1_000_000:06d}"

    def generar_qr(self, codigo: str) -> str | None:
        """
        Crea una imagen PNG con el QR del código. Devuelve la ruta del archivo,
        o None si la librería `qrcode` no está instalada.
        """
        try:
            import qrcode
        except ImportError:
            logger.warning("Librería 'qrcode' no instalada; se omite la imagen QR.")
            return None

        ruta = os.path.join(self.carpeta_qr, f"retiro_{codigo}.png")
        img = qrcode.make(codigo)
        img.save(ruta)
        logger.info("QR generado: %s", ruta)
        return ruta

    def enviar_email_retiro(self, email_destino: str, unidad_id: str,
                            recurso_id: str, codigo: str) -> bool:
        """
        Genera el QR y envía (o simula) el email con las instrucciones de retiro.
        """
        ruta_qr = self.generar_qr(codigo)

        asunto = "Portería Virtual - Tienes una encomienda"
        cuerpo = (
            f"Hola residente de la unidad {unidad_id},\n\n"
            f"Has recibido una encomienda en el casillero {recurso_id}.\n"
            f"Tu código de retiro es: {codigo}\n\n"
            f"Presenta este código (o el QR adjunto) en la pantalla de la "
            f"portería para retirar tu paquete.\n"
        )

        if self.simulado:
            logger.info("=== EMAIL SIMULADO ===")
            logger.info("Para: %s", email_destino)
            logger.info("Asunto: %s", asunto)
            logger.info("Código: %s | QR: %s", codigo, ruta_qr)
            logger.info("======================")
            return True

        return self._enviar_smtp(email_destino, asunto, cuerpo, ruta_qr)

    # ------------------------------------------------------------------ #
    def _enviar_smtp(self, email_destino, asunto, cuerpo, ruta_qr) -> bool:
        """Envío real por SMTP. Activar configurando las variables del .env."""
        import smtplib
        from email.message import EmailMessage

        try:
            msg = EmailMessage()
            msg["Subject"] = asunto
            msg["From"] = os.getenv("EMAIL_REMITENTE")
            msg["To"] = email_destino
            msg.set_content(cuerpo)

            if ruta_qr and os.path.exists(ruta_qr):
                with open(ruta_qr, "rb") as f:
                    msg.add_attachment(f.read(), maintype="image", subtype="png",
                                       filename=os.path.basename(ruta_qr))

            host = os.getenv("SMTP_HOST")
            port = int(os.getenv("SMTP_PORT", "587"))
            with smtplib.SMTP(host, port) as server:
                server.starttls()
                server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
                server.send_message(msg)

            logger.info("Email enviado a %s.", email_destino)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Error enviando email: %s", e)
            return False
