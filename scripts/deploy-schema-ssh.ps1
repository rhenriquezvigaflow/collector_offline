[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Server,

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9._-]*$')]
    [string]$User,

    [ValidateRange(1, 65535)]
    [int]$Port = 22,

    [string]$IdentityFile,

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]*$')]
    [string]$Database = 'crystal_offline',

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]*$')]
    [string]$DatabaseRole = 'collector_local',

    [switch]$Interactive,

    [switch]$UploadOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Server.StartsWith('-')) {
    throw 'El servidor SSH no puede comenzar con "-".'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runnerPath = Join-Path $PSScriptRoot 'apply-initial-schema.sh'
$sqlPath = Join-Path $projectRoot 'migrations\local\001_initial.sql'

foreach ($requiredPath in @($runnerPath, $sqlPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "No se encontró el archivo requerido: $requiredPath"
    }
}

$sshCommand = (Get-Command ssh -ErrorAction Stop).Source
$scpCommand = (Get-Command scp -ErrorAction Stop).Source
$target = if ($User) { "$User@$Server" } else { $Server }

$sshArgs = @(
    '-p', $Port.ToString(),
    '-o', 'ConnectTimeout=10'
)
$scpArgs = @(
    '-P', $Port.ToString(),
    '-o', 'ConnectTimeout=10'
)
if (-not $Interactive) {
    $sshArgs += @('-o', 'BatchMode=yes')
    $scpArgs += @('-o', 'BatchMode=yes')
}

if ($IdentityFile) {
    $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArgs += @('-i', $resolvedIdentity)
    $scpArgs += @('-i', $resolvedIdentity)
}

Write-Host "Comprobando conexión SSH con $target..."
& $sshCommand @sshArgs $target 'true'
if ($LASTEXITCODE -ne 0) {
    throw "No fue posible conectar por SSH con $target."
}

$remoteDirOutput = & $sshCommand @sshArgs $target `
    'mktemp -d /tmp/collector-offline-schema.XXXXXX'
if ($LASTEXITCODE -ne 0) {
    throw 'No fue posible crear el directorio temporal remoto.'
}
$remoteDir = ($remoteDirOutput | Select-Object -Last 1).Trim()
if ($remoteDir -notmatch '^/tmp/collector-offline-schema\.[A-Za-z0-9]+$') {
    throw "El servidor devolvió una ruta temporal inesperada: $remoteDir"
}

Write-Host "Copiando migración a ${target}:$remoteDir/ ..."
& $scpCommand @scpArgs $runnerPath $sqlPath "${target}:$remoteDir/"
if ($LASTEXITCODE -ne 0) {
    throw 'Falló la copia del script Linux o de 001_initial.sql.'
}

$remotePrepare = @"
chmod 0755 '$remoteDir'
chmod 0750 '$remoteDir/apply-initial-schema.sh'
chmod 0644 '$remoteDir/001_initial.sql'
sha256sum '$remoteDir/apply-initial-schema.sh' '$remoteDir/001_initial.sql'
"@ -replace "`r?`n", ' && '
$remoteHashes = & $sshCommand @sshArgs $target $remotePrepare
if ($LASTEXITCODE -ne 0) {
    throw 'No fue posible preparar o verificar los archivos remotos.'
}

$localHashes = @{
    'apply-initial-schema.sh' = (Get-FileHash -Algorithm SHA256 -LiteralPath $runnerPath).Hash.ToLowerInvariant()
    '001_initial.sql' = (Get-FileHash -Algorithm SHA256 -LiteralPath $sqlPath).Hash.ToLowerInvariant()
}
foreach ($fileName in $localHashes.Keys) {
    $matchingLine = $remoteHashes | Where-Object { $_ -match "/$([regex]::Escape($fileName))$" }
    if (-not $matchingLine) {
        throw "El servidor no devolvió el hash de $fileName."
    }
    $remoteHash = (($matchingLine -split '\s+')[0]).ToLowerInvariant()
    if ($remoteHash -ne $localHashes[$fileName]) {
        throw "El hash remoto de $fileName no coincide con el archivo local."
    }
}

Write-Host "Archivos transferidos y verificados en $remoteDir."
if ($UploadOnly) {
    Write-Host 'UploadOnly activo; no se modificó PostgreSQL.'
    Write-Host "Para ejecutar: sudo bash $remoteDir/apply-initial-schema.sh --sql $remoteDir/001_initial.sql"
    exit 0
}

$remoteExecute = @"
sudo$(if ($Interactive) { '' } else { ' -n' }) bash '$remoteDir/apply-initial-schema.sh'
  --database '$Database'
  --role '$DatabaseRole'
  --sql '$remoteDir/001_initial.sql'
"@ -replace "`r?`n\s*", ' '

Write-Host "Creando/verificando la base y las tablas en $target..."
$executionSshArgs = @($sshArgs)
if ($Interactive) {
    $executionSshArgs += '-t'
}
& $sshCommand @executionSshArgs $target $remoteExecute
if ($LASTEXITCODE -ne 0) {
    throw @"
La ejecución remota falló. Los archivos permanecen en $remoteDir.
Si el servidor exige contraseña sudo, entre por SSH y ejecute:
  sudo bash $remoteDir/apply-initial-schema.sh --database $Database --role $DatabaseRole --sql $remoteDir/001_initial.sql
"@
}

Write-Host 'Despliegue de esquema completado correctamente.'
