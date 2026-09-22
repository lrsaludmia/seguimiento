# ---------------------------------------------------------------------------
# publicar.ps1 — Regenera el Tablero Semanal desde Linear y lo publica en Pages.
#
# Qué hace:
#   1. Genera docs/index.html (y la copia histórica en reportes/) con datos
#      frescos del ciclo activo de Linear.
#   2. Si hubo cambios, hace commit y push -> GitHub Pages redespliega solo.
#
# Requisitos:
#   - Variable de entorno LINEAR_API_KEY (Personal API Key de Linear).
#   - git con credenciales guardadas (Administrador de credenciales de Windows
#     o SSH) para poder hacer push sin que pida usuario/clave.
#
# Uso manual:   powershell -ExecutionPolicy Bypass -File publicar.ps1
# Automático:   ver el bloque de Programador de tareas en README.md
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/3] Generando el tablero desde Linear..."
python "generar_reporte_ciclo.py" --tablero
if ($LASTEXITCODE -ne 0) { throw "Fallo al generar el tablero (exit $LASTEXITCODE)" }

Write-Host "[2/3] Revisando cambios..."
git add docs
$pending = git status --porcelain docs
if (-not $pending) {
    Write-Host "Sin cambios en docs/. Nada que publicar."
    exit 0
}

Write-Host "[3/3] Publicando..."
$fecha = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "Tablero semanal: actualizacion $fecha"
git push
Write-Host "Listo. GitHub Pages redesplegara en ~1 minuto."
