# Portería Virtual

Sistema de gestión automatizada de lockers y buzones para condominios, sobre
Raspberry Pi 3B con pantalla táctil, relés GPIO y Firebase Firestore.

## Arquitectura

| Archivo                   | Responsabilidad                                          |
|---------------------------|----------------------------------------------------------|
| `config.json`             | Tipo de sistema, modalidad de puertas y pines GPIO.      |
| `config_manager.py`       | Carga y **valida** la configuración.                     |
| `hardware_manager.py`     | Controla relés/cerraduras (GPIO real o **mock**).        |
| `firebase_service.py`     | Conexión y consultas a Firestore.                        |
| `resource_allocator.py`   | Asigna locker/buzón según tamaño y disponibilidad.       |
| `notification_service.py` | Genera QR y envía email de retiro (simulado).            |
| `main.py`                 | GUI Tkinter y orquestación del flujo.                    |

## Requisitos

- **Python 3.9+** (el código usa `from __future__ import annotations`).
- En Raspberry Pi OS: `sudo apt-get install python3-tk`

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # completar credenciales
```

En la **Raspberry Pi**, además descomentar `RPi.GPIO` en `requirements.txt`.

## Ejecución

```bash
python main.py
```

- En un **PC sin GPIO**, el `HardwareManager` usa un *mock* que imprime las
  acciones de las cerraduras por consola. No requiere hardware.
- Si Firebase no está disponible, la app muestra **"Servicio en mantención"**.

## Pruebas rápidas de módulos

```bash
python config_manager.py     # valida el config.json
python hardware_manager.py   # simula accionar cerraduras
python firebase_service.py   # prueba conexión (requiere .env)
```

## Configuración fija actual

- Tipo: **mixto** — 6 lockers + 1 buzón.
- Operación de puertas: **bidireccional** (2 puertas por locker).
- Buzón: **2 cerraduras físicas** (depósito y retiro), siempre.

## Modelo de datos en Firestore

```
unidades/{unidad_id}
    {
      activo: bool,
      torre: str,
      residentes: [
        { nombre: "Juan Pérez",  email: "juan@correo.cl" },
        { nombre: "María López", email: "maria@correo.cl" }
      ]
    }

entregas/{auto_id}
    { unidad_id, recurso_id, tipo_recurso, tamano, destinatario,
      email_destino, codigo_retiro, estado: "depositada"|"retirada",
      fecha_deposito, fecha_retiro }
```

> `tipo_unidad` en `config.json` (`"departamento"` | `"casa"`) define la
> etiqueta que ve el repartidor: **"N° Depto"** o **"N° Casa"**.

## Modalidades de retiro (según `operacion_puertas`)

**Caso 1 — `unidireccional` (1 puerta):** el repartidor y el residente usan la
**misma pantalla y puerta**. El residente toca *Retirar* y escanea el QR.

**Caso 2 — `bidireccional` (2 puertas):** cada locker tiene una puerta de
**depósito (exterior)** y otra de **retiro (interior)**. El repartidor deposita
por fuera sin entrar al edificio; el residente retira por dentro. Para el retiro
interior se usa un **lector QR dedicado sin pantalla**: al escanear un código
vigente, la puerta de retiro se abre automáticamente. La pantalla exterior sigue
ofreciendo el flujo tradicional (ambos botones) como respaldo.

> Esta modalidad aplica a lockers (y puede incluir un buzón). El QR escaneado es
> el `parcel_id` (mismo valor que muestra la app del residente).

### Habilitar el retiro automático (Caso 2, solo Raspberry/Linux)

1. Instalar la librería: `pip install evdev`
2. Identificar el dispositivo del lector dedicado:
   ```bash
   ls -l /dev/input/by-id/         # buscar el ...-event-kbd del lector
   # o para descubrir cuál es:
   python3 -c "import evdev; [print(d.path, d.name) for d in map(evdev.InputDevice, evdev.list_devices())]"
   ```
3. En `config.json` → `retiro_automatico`:
   ```json
   { "habilitado": true, "dispositivo": "/dev/input/by-id/usb-XXXX-event-kbd" }
   ```
4. Dar permiso de lectura al dispositivo (agregar el usuario al grupo `input`):
   `sudo usermod -aG input $USER`  (y reiniciar sesión).

En Windows/Mac (desarrollo) `evdev` no existe: el lector queda **inactivo** sin
romper la app; el retiro se prueba por pantalla.

## Pendiente

- (Opcional) Notificación push/WhatsApp al residente al depositar (la app de
  producción ya tiene infraestructura FCM en `server.cjs`).
- (Opcional) Feedback físico (beep/LED) en el retiro automático sin pantalla.

## Flujos implementados

- **Dejar Encomienda:** N° Depto/Casa → selección de residente (solo del
  condominio) → tamaño (si mixto) → abrir depósito → registrar en SQLite local →
  sincronizar a Firebase. El residente ve el QR de retiro en su app.
- **Retirar Encomienda:** por pantalla (escanear/ingresar) o por lector
  dedicado automático → abrir retiro → marcar retirada → sincronizar.
```
