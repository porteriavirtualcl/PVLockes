# Instalación del kiosco en la Raspberry Pi 3B

Guía paso a paso para dejar el kiosco "Portería Virtual" corriendo en una
Raspberry Pi 3B con **Raspberry Pi OS (Legacy, 32-bit)** (Debian Bookworm).

> Nota GPIO: en Bookworm/Trixie el `RPi.GPIO` clásico no corre nativo; se usa el
> shim **rpi-lgpio** (mismo `import RPi.GPIO`, por debajo lgpio). El código NO cambia.

---

## 0. Grabar la SD (Raspberry Pi Imager)

1. SO: **Raspberry Pi OS (Legacy, 32-bit)**.
2. En **Personalización (⚙ OPCIONES)** preconfigurar:
   - **Hostname**: ej. `kiosk-losaromos-01`
   - **Usuario y contraseña**
   - **Wi-Fi**: SSID + clave (país: CL)
   - **SSH**: activado (con contraseña)
   - **Zona horaria**: `America/Santiago` · **Teclado**: `es`
3. Escribir y arrancar la Pi con la SD.

---

## 1. Primer arranque y actualización

Por SSH (`ssh usuario@kiosk-losaromos-01.local`) o con teclado/monitor:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 2. Dependencias del sistema (apt)

```bash
sudo apt install -y git python3-venv python3-tk python3-rpi-lgpio python3-evdev
```

- `python3-tk` → GUI Tkinter
- `python3-rpi-lgpio` → **shim GPIO** (provee `import RPi.GPIO`)
- `python3-evdev` → lector QR de retiro automático (opcional)
- `git`, `python3-venv` → clonar y entorno virtual

---

## 3. Clonar el proyecto

```bash
cd ~
git clone https://github.com/porteriavirtualcl/PVLockes.git
cd PVLockes
```

> Si el repo es privado: `gh auth login` (instalar `gh`) o usar un token de
> acceso personal al clonar.

---

## 4. Entorno virtual + dependencias Python

Importante: crear el venv con `--system-site-packages` para que vea los paquetes
GPIO/evdev instalados por apt.

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install firebase-admin python-dotenv "qrcode[pil]"
```

> NO instalar `RPi.GPIO` por pip (rompería el shim). El `import RPi.GPIO` ya lo
> resuelve `python3-rpi-lgpio` (apt).

Prueba rápida (sin hardware real igual funciona con lockers reales por GPIO):

```bash
python config_manager.py     # valida config.json
python main.py               # abre la GUI
```

---

## 5. Credenciales de Firebase

```bash
mkdir -p ~/PVLockes/secrets
# Copiar el JSON de la service account a:
#   ~/PVLockes/secrets/firebase-service-account.json
cp .env.example .env
nano .env
```

En `.env`:
```
FIREBASE_CREDENTIALS_PATH=./secrets/firebase-service-account.json
FIREBASE_PROJECT_ID=porteriavitual
```

> `secrets/` y `.env` están en `.gitignore` — nunca se suben al repo.

---

## 6. Configurar este equipo (`config.json`)

```bash
nano config.json
```

- `kiosk_id`: el id real (debe coincidir con `kiosks/{kiosk_id}` en la app).
- `condominio.condo_id` / `condo_name`: el condominio real.
- `pines` en `recursos`: verificar que coincidan con el cableado físico.

> El resto de la config lógica (tipo, tamaños, orientación) puede administrarse
> desde la app (colección `kiosks/{kiosk_id}`) y el kiosco la aplica al iniciar.

---

## 7. Pantalla vertical (rotación)

Editar el config de arranque:

```bash
sudo nano /boot/firmware/config.txt
```

Agregar al final (rotación 90°):
```
display_rotate=1
```

> El método exacto varía según el modelo de pantalla (DSI oficial vs HDMI). Si
> `display_rotate` no rota el táctil, usar la herramienta gráfica *Screen
> Configuration* del escritorio, o calibrar el touch aparte. Reiniciar tras el cambio.

También conviene **desactivar el apagado de pantalla** (kiosco siempre encendido):
en el escritorio → Preferencias → Screensaver → desactivar; o instalar `xscreensaver`
y desactivarlo.

---

## 8. Autostart al encender

Que el kiosco arranque solo al prender la Pi. Primero un script lanzador:

```bash
nano ~/PVLockes/run_kiosk.sh
```
```bash
#!/bin/bash
cd /home/USUARIO/PVLockes
source venv/bin/activate
python main.py
```
(reemplazar `USUARIO`)

```bash
chmod +x ~/PVLockes/run_kiosk.sh
```

**Opción A — XDG autostart (LXDE/X11):**
```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/porteria.desktop
```
```
[Desktop Entry]
Type=Application
Name=Porteria Virtual Kiosk
Exec=/home/USUARIO/PVLockes/run_kiosk.sh
X-GNOME-Autostart-enabled=true
```

**Opción B — Wayland (labwc, por si A no aplica):**
```bash
mkdir -p ~/.config/labwc
echo "/home/USUARIO/PVLockes/run_kiosk.sh &" >> ~/.config/labwc/autostart
```

**Opción C — Wayland (wayfire):** en `~/.config/wayfire.ini`:
```
[autostart]
porteria = /home/USUARIO/PVLockes/run_kiosk.sh
```

> Bookworm usa Wayland en algunas configuraciones y X11 en otras. Si la Opción A
> no arranca la app, usar B o C según el compositor activo (`echo $XDG_SESSION_TYPE`).

Reiniciar y verificar:
```bash
sudo reboot
```

---

## 9. Verificación final

- Al encender, la GUI del kiosco debe abrir sola, en vertical.
- Log del kiosco: se ve por consola (o redirigir a archivo en `run_kiosk.sh`:
  `python main.py >> ~/PVLockes/kiosk.log 2>&1`).
- Con internet + credenciales, el SyncService baja residentes/couriers/config y
  sube encomiendas. "🟢 En línea" en el pie de la pantalla principal.
- Probar: dejar una encomienda → aparece en la app; retirar por QR/lector.

---

## Actualizar el kiosco a futuro

```bash
cd ~/PVLockes
git pull
source venv/bin/activate
pip install -r requirements.txt   # si hubo nuevas dependencias
sudo reboot
```
