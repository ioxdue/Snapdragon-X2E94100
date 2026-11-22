<#
.SYNOPSIS
    Launcher integrato per Hexagon Local Coding Agent con Kilo Code e runtime Hexagon GGUF.

.DESCRIPTION
    Verifica l'endpoint locale del router Hexagon (default: 127.0.0.1:18803), avvia il router
    se non e' gia' in esecuzione, allinea la configurazione kilo.json ai modelli effettivamente
    disponibili e avvia la CLI Kilo Code con provider 100% locale.

.PARAMETER Port
    Porta HTTP del router llama-server (default: 18803).

.PARAMETER BindHost
    Indirizzo bind del router (default: 127.0.0.1, strettamente loopback).

.PARAMETER Model
    Modello principale per Kilo (default: "hexagon/Ornith-1.5-9B-Q4_0").

.PARAMETER SmallModel
    Modello compatto ausiliario (default: "hexagon/MiniCPM5-2B-Q4_K_M").

.PARAMETER Agent
    Ruolo agent iniziale da avviare (es. code, plan, debug, explore, reviewer).

.PARAMETER Message
    Esegue un messaggio singolo non interattivo (kilo run) ed esce.

.PARAMETER CheckOnly
    Verifica router, modelli e configurazione senza avviare Kilo.

.PARAMETER GenerateConfigOnly
    Rigenera kilo.json ed esce.

.PARAMETER NoAutoStart
    Non avviare automaticamente il router se offline.

.PARAMETER Pure
    Esegue Kilo in modalita' pure (senza plugin esterni).
#>

param(
    [int]$Port = 18803,
    [string]$BindHost = "127.0.0.1",
    [string]$Model = "hexagon/Ornith-1.5-9B-Q4_0",
    [string]$SmallModel = "hexagon/MiniCPM5-2B-Q4_K_M",
    [string]$Agent = "",
    [string]$Message = "",
    [switch]$CheckOnly,
    [switch]$GenerateConfigOnly,
    [switch]$NoAutoStart,
    [switch]$Pure
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " HEXAGON LOCAL CODING AGENT (Kilo Code + Hexagon GGUF)" -ForegroundColor Cyan
Write-Host " Target: Windows 11 ARM64 (Qualcomm Snapdragon)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# 1. Verifica eseguibile Kilo
$KiloCmd = Get-Command "kilo.cmd", "kilo" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $KiloCmd) {
    Write-Error "Kilo Code CLI ('kilo' o 'kilo.cmd') non trovato nel PATH. Esegui .\setup-kilo.ps1 per i controlli dei prerequisiti."
    exit 1
}
Write-Host " * Frontend:          Kilo Code CLI ($($KiloCmd.Source))" -ForegroundColor Green

# 2. Verifica Router Hexagon
$BaseUrl = "http://${BindHost}:${Port}/v1"
Write-Host " * Endpoint Router:   $BaseUrl" -ForegroundColor Green

$IsOnline = $false
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/models" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($resp -and $resp.data) {
        $IsOnline = $true
        $ModelCount = $resp.data.Count
        Write-Host " * Stato Router:      Online ($ModelCount modelli registrati)" -ForegroundColor Green
    }
} catch {
    $IsOnline = $false
}

if (-not $IsOnline) {
    if ($CheckOnly) {
        Write-Host " * Stato Router:      Offline" -ForegroundColor Yellow
        Write-Host "Verifica completata: Router Hexagon non in esecuzione su $BaseUrl."
        exit 1
    }

    if ($NoAutoStart) {
        Write-Error "Router Hexagon non attivo su $BaseUrl e -NoAutoStart specificato."
        exit 1
    }

    Write-Host " * Stato Router:      Offline. Avvio automatico router locale in corso..." -ForegroundColor Yellow
    $LaunchHexagon = Join-Path $ScriptDir "launch-hexagon.ps1"
    if (-not (Test-Path $LaunchHexagon)) {
        Write-Error "Script launch-hexagon.ps1 non trovato in $ScriptDir"
        exit 1
    }

    # Avvio router in background console separata
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$LaunchHexagon`" -Mode Router -Port $Port -BindHost $BindHost" -WorkingDirectory $ScriptDir

    Write-Host " In attesa del router su $BaseUrl..." -ForegroundColor Cyan
    $Retries = 20
    while ($Retries -gt 0 -and -not $IsOnline) {
        Start-Sleep -Seconds 1
        try {
            $resp = Invoke-RestMethod -Uri "$BaseUrl/models" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($resp -and $resp.data) {
                $IsOnline = $true
                $ModelCount = $resp.data.Count
                Write-Host " Router attivo con successo! ($ModelCount modelli)" -ForegroundColor Green
                break
            }
        } catch { }
        $Retries--
    }

    if (-not $IsOnline) {
        Write-Error "Impossibile contattare il router Hexagon su $BaseUrl entro il timeout."
        exit 1
    }
}

# 3. Sincronizzazione kilo.json
$KiloConfigScript = Join-Path $ScriptDir "kilo_config.py"
if (Test-Path $KiloConfigScript) {
    Write-Host " * Sincronizzazione:  Aggiornamento kilo.json..." -ForegroundColor Gray
    python $KiloConfigScript --generate --port $Port
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "kilo_config.py ha restituito codice $LASTEXITCODE. Verifica kilo.json."
    }
}

if ($GenerateConfigOnly) {
    Write-Host "Configurazione kilo.json generata con successo." -ForegroundColor Green
    exit 0
}

if ($CheckOnly) {
    Write-Host "`nControllo stato completato con successo:" -ForegroundColor Green
    python $KiloConfigScript --status --port $Port
    exit 0
}

# 4. Avvio di Kilo Code
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host " Modello Principale:  $Model" -ForegroundColor White
Write-Host " Modello Ausiliario:  $SmallModel" -ForegroundColor White
if ($Agent) {
    Write-Host " Ruolo Agente:        $Agent" -ForegroundColor White
}
Write-Host "========================================================`n" -ForegroundColor Cyan

$kiloArgs = @()
if ($Pure) {
    $kiloArgs += "--pure"
}

if ($Message -ne "") {
    Write-Host "Esecuzione prompt singolo non interattivo..." -ForegroundColor Cyan
    $runArgs = @("run", $Message, "-m", $Model)
    if ($Agent) {
        $runArgs += @("--agent", $Agent)
    }
    if ($Pure) {
        $runArgs += "--pure"
    }
    & $KiloCmd.Source @runArgs
    exit $LASTEXITCODE
} else {
    $startArgs = @("-m", $Model)
    if ($Agent) {
        $startArgs += @("--agent", $Agent)
    }
    if ($Pure) {
        $startArgs += "--pure"
    }
    & $KiloCmd.Source @startArgs
    exit $LASTEXITCODE
}
