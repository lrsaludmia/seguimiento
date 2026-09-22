# Seguimiento por ciclo — SaludMía (Linear)

Genera dashboards **HTML autocontenidos** con el desempeño del equipo en el ciclo
activo de Linear. Todo el cálculo y el render los hace el equipo local (Python + un
poco de JS en el navegador) con **~0 tokens de modelo**.

Hay **dos salidas**:

| Salida | Comando | Diseño | Para qué |
|--------|---------|--------|----------|
| **Tablero Semanal** | `--tablero` | Identidad SaludMía (verde), pensado para compartir | Publicar en la web para el equipo y la jefatura |
| **Reporte técnico** | (por defecto) | Sobrio, claro/oscuro, con burnup y throughput | Análisis interno detallado |

El **Tablero Semanal** es el que se publica en GitHub Pages (`docs/index.html`).

## Requisitos

- Python 3.8+ (solo librería estándar).
- Una **Personal API Key** de Linear.
- Para publicar: `git` y una cuenta de GitHub.

### Obtener la API Key

1. Linear → **Settings → Security & access → Personal API keys → New key**.
2. Copia el token (empieza con `lin_api_`).
3. Guárdalo como variable de entorno (PowerShell, persistente):

   ```powershell
   setx LINEAR_API_KEY "lin_api_xxxxxxxxxxxxxxxx"
   ```

   Cierra y reabre la terminal para que tome efecto.

> El equipo se identifica por su **clave** `SAL` (el prefijo de los issues, p.ej.
> `SAL-228`), no por el nombre. Está en `TEAM_KEY` dentro de `generar_reporte_ciclo.py`.

## Uso

```powershell
# Tablero Semanal desde Linear (ciclo activo) -> reportes/ y docs/index.html:
python generar_reporte_ciclo.py --tablero

# Abrir el HTML al terminar:
python generar_reporte_ciclo.py --tablero --abrir

# Sin API key, desde un snapshot (para pruebas):
python generar_reporte_ciclo.py --tablero --datos datos/ciclo_23.json

# Reporte técnico clásico (burnup/throughput), sin --tablero:
python generar_reporte_ciclo.py
```

### Qué muestra el Tablero Semanal

1. **Estado de avance vs. planeación** — finalizadas, en curso, detenidas, planeadas,
   totales; % de avance real vs. lo esperado a la fecha.
2. **Último update por proyecto** — el project update más reciente de Linear con su
   salud (Al día / En riesgo / Atrasado).
3. **Cronología del ciclo** — barras por tarea sobre los días hábiles del ciclo, con
   duración real vs. teórica.
4. **Tareas por responsable** — planeadas / finalizadas / en curso, % de resolución y
   carga en paralelo.
5. **Top 5 proyectos** con más tareas.
6. **Salud de datos** — % con estimación, con etiqueta de tipo (Doble Carril), sin
   asignar, completadas sin fecha de inicio.
7. **Detenidas esperando respuesta** — tareas en el estado «Esperando respuesta».

El tablero se alimenta de la plantilla `Tablero de seguimiento Linear/plantilla_tablero.html`
(la lógica de cálculo vive en `renderVals`, en esa plantilla; Python solo inyecta los
datos crudos del ciclo).

## Publicar en GitHub Pages

> **Privacidad:** la URL de GitHub Pages es **pública** (cualquiera con el enlace la
> ve; el Pages privado solo existe en GitHub Enterprise). Como el tablero muestra
> nombres y desempeño, se aplican mitigaciones: `<meta name="robots" content="noindex,nofollow">`
> en la página y `docs/robots.txt`. Eso reduce el descubrimiento en buscadores, pero
> **no es control de acceso**. Si más adelante hace falta acceso restringido de verdad,
> se migra a **Cloudflare Pages + Access** (gratis, login por correo) sin tocar el
> generador.

Configuración única:

1. **Identidad de git** (una sola vez, si no la tienes):

   ```powershell
   git config --global user.name "Tu Nombre"
   git config --global user.email "tu-correo@saludmia.org"
   ```

2. **Crear el repositorio en GitHub** (por la web: New repository → nómbralo p.ej.
   `seguimiento-linear` → puede ser **privado**; el sitio de Pages seguirá siendo
   público). Luego, en esta carpeta:

   ```powershell
   git add -A
   git commit -m "Tablero semanal inicial"
   git branch -M main
   git remote add origin https://github.com/<usuario>/seguimiento-linear.git
   git push -u origin main
   ```

3. **Activar Pages:** repo → **Settings → Pages → Build and deployment → Source:
   Deploy from a branch → Branch: `main` / carpeta `/docs`** → Save.

4. A los ~1–2 min el tablero queda en
   `https://<usuario>.github.io/seguimiento-linear/`.

## Automatización semanal (0 tokens)

`publicar.ps1` regenera el tablero desde Linear y, si hubo cambios, hace `commit` +
`push` (Pages redespliega solo). Requiere que git tenga credenciales guardadas
(Administrador de credenciales de Windows) para el push sin prompt.

Prueba manual:

```powershell
powershell -ExecutionPolicy Bypass -File publicar.ps1
```

Programarlo cada semana con el **Programador de tareas de Windows** (ej. lunes 8:00):

```powershell
schtasks /Create /TN "SaludMia-TableroSemanal" /SC WEEKLY /D MON ^
  /ST 08:00 ^
  /TR "powershell -ExecutionPolicy Bypass -File \"C:\Users\luisruiz\Documents\Salud Mia\Productos\seguimiento_linear\publicar.ps1\""
```

- Verifica con `schtasks /Query /TN "SaludMia-TableroSemanal"`.
- Elimina con `schtasks /Delete /TN "SaludMia-TableroSemanal" /F`.

> Alternativa descartada: un agente en la nube (cron de Claude) gastaría tokens en cada
> corrida. El script determinista no.

## Estructura

```
seguimiento_linear/
├─ generar_reporte_ciclo.py            # generador: fetch Linear + métricas + render
├─ publicar.ps1                        # regenera + git push (automatización)
├─ datos/ciclo_23.json                 # snapshot de ejemplo (pruebas sin API)
├─ reportes/                           # salidas HTML por ciclo (histórico)
├─ docs/                               # lo que publica GitHub Pages
│  ├─ index.html                       # último Tablero Semanal
│  ├─ robots.txt                       # noindex a nivel carpeta
│  └─ .nojekyll                        # sirve el HTML tal cual
└─ Tablero de seguimiento Linear/
   ├─ plantilla_tablero.html           # plantilla del Tablero Semanal (marcador /*__DATA__*/)
   ├─ Tablero Semanal Linear.local.html# versión con datos de muestra (referencia)
   ├─ ds/                              # sistema de diseño SaludMía
   └─ support.js                       # runtime del componente de diseño (referencia)
```

## Regla clave: throughput real vs. carryover (reporte técnico)

Un issue completado cuenta como **carryover** (limpieza de backlog) cuando se cerró
**el primer día del ciclo** y fue **creado antes** de que el ciclo iniciara. El resto
son **throughput real**. Esto evita inflar el desempeño con cierres de backlog viejo.
