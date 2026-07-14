# Fase 0 — Modelo de configuración de dispositivos (lockers/buzones)

Diseño para administrar cada kiosco (locker/buzón) desde el **backend**
(Firestore), gestionado por un **super_admin** en el módulo Encomiendas de la
app de producción. El kiosco lee su configuración lógica desde Firestore y la
combina con su configuración física local.

## Principios

- **Firestore es la "API".** El kiosco (Python) se conecta directo con el
  **Admin SDK / service account**, que **omite las reglas de seguridad**. No se
  necesita construir una API REST nueva.
- **Lógica remota, hardware local.** Lo que define *cómo opera* el equipo se
  administra central en Firestore; lo *físico* (pines GPIO, lector) queda en el
  `config.json` del equipo.
- **Offline-first.** El kiosco cachea su config remota; si no hay red usa la
  última conocida (o el `config.json` local como fallback).

## Colección: `kiosks/{kioskId}`

Un documento por equipo. El `kioskId` es el identificador único del dispositivo
(también guardado en el `config.json` local para saber qué doc leer).

```jsonc
// kiosks/kiosk-losaromos-01
{
  "kioskId": "kiosk-losaromos-01",         // = id del documento
  "nombre": "Lockers Torre A - Acceso principal",
  "condoId": "AbCdEf123",                   // condominio (doc de condos/{id})
  "condoName": "Condominio Los Aromos",
  "activo": true,

  // --- Configuración lógica (lo que hoy está en config.json → sistema) ---
  "tipo": "mixto",                          // "locker" | "buzon" | "mixto"
  "operacionPuertas": "bidireccional",      // "unidireccional" | "bidireccional"
  "tipoUnidad": "departamento",             // "departamento" | "casa"
  "orientacion": "vertical",                // "vertical" | "horizontal"
  "retiroAutomatico": true,                 // habilita el lector dedicado (Caso 2)

  // --- Recursos LÓGICOS (sin pines; los pines viven en el equipo) ---
  "lockers": [
    { "id": "L1", "tamano": "mediana" },
    { "id": "L2", "tamano": "mediana" },
    { "id": "L3", "tamano": "grande"  },
    { "id": "L4", "tamano": "grande"  },
    { "id": "L5", "tamano": "mediana" },
    { "id": "L6", "tamano": "grande"  }
  ],
  "buzon": { "id": "B1", "tamano": "chica" }, // o null si no tiene

  "updatedAt": "<serverTimestamp>",
  "updatedBy": "<uid del super_admin>"
}
```

### (Opcional, Fase 3) Estado reportado por el kiosco

El kiosco puede escribir su estado (Admin SDK) para monitoreo en la app:

```jsonc
{
  "estado": {
    "online": true,
    "ultimaConexion": "<timestamp>",
    "pendientesSync": 0,
    "ocupacion": ["L3", "L4"]              // recursos con encomienda en portería
  }
}
```

## Qué queda en el equipo (`config.json` local)

El `config.json` local guarda lo **físico y propio del equipo**, y además sirve
de **fallback completo** si aún no hay config remota:

- `kiosk_id` — identifica el equipo (qué doc `kiosks/{kiosk_id}` leer).
- `gpio` — modo, relé activo-en-bajo, duración de pulso.
- `recursos` — lockers y buzón **con sus pines** (`pin_deposito`/`pin_retiro`).
  Es la fuente física de pines Y el fallback lógico si no hay remoto.
- `retiro_automatico.dispositivo` — ruta evdev del lector dedicado.
- `sincronizacion` — BD local, intervalo.
- `sistema`, `pantalla`, `couriers`, `condominio` — valores de fallback.

**Merge implementado (Fase 2):** al iniciar, el kiosco aplica la config remota
cacheada sobre la local:

- Lo **lógico** viene del remoto: `tipo`, `operacionPuertas`, `tipoUnidad`,
  `orientacion`, `retiroAutomatico`, `condoId`/`condoName`.
- Los **recursos** se reconstruyen cruzando por `id`: cada locker/buzón definido
  en Firestore toma sus **pines del `recursos` local** y su **tamaño del remoto**.
  Un `id` remoto sin pines locales se **omite** (cableado faltante, se registra).
- Si el merge resulta incoherente (ej. falla la validación), se **conserva la
  config local** intacta (revert automático).
- Los pines **nunca** viajan por la red.

> **Cuándo aplica:** la config remota se **cachea** en cada ciclo de sync y se
> **aplica al próximo reinicio** del kiosco (no en caliente, para no reconfigurar
> GPIO en vivo). Un cambio en el backend se refleja tras reiniciar el equipo.

## Reglas de seguridad (agregar a `pvsoftware/firestore.rules`)

El kiosco (Admin SDK) **no** pasa por estas reglas; solo gobiernan a la app.

```
// Kiosks / dispositivos de lockers-buzones.
// Config lógica administrada por super_admin desde la app.
// El kiosco (Admin SDK / service account) lee y escribe SIN pasar por estas reglas.
match /kiosks/{kioskId} {
  allow read:  if isAuthenticated();     // staff puede ver los equipos
  allow write: if isSuperAdmin();        // solo super_admin administra
}
```

> Se sigue el mismo patrón que `config/{docId}` (lectura autenticada, escritura
> super_admin) y que el modal *"Manage courier companies"* ya existente.

## Fases siguientes

- **Fase 1 (app producción):** botón *"⚙ Configuración de Lockers/Buzones"* en
  Encomiendas (solo super_admin) → CRUD de documentos `kiosks/{kioskId}`.
  Mismo patrón de modal que couriers (`Parcels.tsx:861`).
- **Fase 2 (kiosco):** leer `kiosks/{kiosk_id}` al sincronizar, cachear en
  SQLite, y combinar con los pines locales. Fallback a `config.json` si nunca
  sincronizó.
- **Fase 3 (opcional):** el kiosco reporta `estado` para monitoreo en la app.
