<#
.SYNOPSIS
    Script di setup, verifica prerequisiti e validazione offline per Hexagon Local Coding Agent.

.DESCRIPTION
    Verifica l'ambiente Windows 11 ARM64, la presenza di Node.js e Kilo Code CLI,
    l'allineamento del runtime Hexagon (GenieX / llama.cpp), la disponibilita' dei modelli GGUF
    e la conformita' delle impostazioni di sicurezza offline.

.PARAMETER VerifyOffline
    Esegue controlli rigorosi sull'assenza di endpoint esterni, chiavi cloud o instradamenti remoti.

.PARAMETER CheckOnly
    Solo verifica in sola lettura senza apportare modifiche.
#>

param(
    [switch]$VerifyOffline,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " SETUP E VERIFICA PREREQUISITI: HEXAGON LOCAL CODING AGENT" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$Passed = 0
$Failed = 0
$Warnings = 0

function Report-Check([string]$Name, [bool]$Ok, [string]$Details = "") {
    if ($Ok) {
        Write-Host " [OK]      $Name $(if ($Details) { "($Details)" })" -ForegroundColor Green
        $script:Passed++
    } else {
        Write-Host " [ERRORE]  $Name $(if ($Details) { "($Details)" })" -ForegroundColor Red
        $script:Failed++
    }
}

function Report-Warn([string]$Name, [string]$Details = "") {
    Write-Host " [ATTENZ]  $Name $(if ($Details) { "($Details)" })" -ForegroundColor Yellow
    $script:Warnings++
}

# 1. Piattaforma e Architettura
$IsWin = [System.Environment]::OSVersion.Platform -eq "Win32NT"
$Arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$IsArm64 = ($Arch -eq "Arm64")
Report-Check "Piattaforma Windows 11 ARM64" ($IsWin -and $IsArm64) "OS: Windows, Arch: $Arch"

# 2. Node.js e NPM
$NodeCmd = Get-Command "node" -ErrorAction SilentlyContinue | Select-Object -First 1
$NodeVer = if ($NodeCmd) { (& $NodeCmd.Source -v 2>$null).Trim() } else { "" }
Report-Check "Node.js installato" ($null -ne $NodeCmd) "Versione: $NodeVer"

# 3. Kilo Code CLI upstream
$KiloCmd = Get-Command "kilo.cmd", "kilo" -ErrorAction SilentlyContinue | Select-Object -First 1
$KiloVer = if ($KiloCmd) { (& $KiloCmd.Source --version 2>$null).Trim() } else { "" }
Report-Check "Kilo Code CLI" ($null -ne $KiloCmd) "Versione: $KiloVer"

# 4. Runtime Hexagon / GenieX via stack_manager.py
$PyCmd = "import stack_manager; sm = stack_manager.StackManager(); st = sm.get_stack_status(); print('|'.join([str(st['geniex_installed']), str(st['llama_server_installed']), str(st['commit_aligned']), str(st['all_files_aligned']), str(st['geniex_version']), str(st['llama_server_version'])]))"
$StackOutput = python -c $PyCmd 2>$null
if ($StackOutput) {
    $parts = $StackOutput.Trim().Split("|")
    $geniexOk = [bool]::Parse($parts[0])
    $llamaOk = [bool]::Parse($parts[1])
    $commitAligned = [bool]::Parse($parts[2])
    $filesAligned = [bool]::Parse($parts[3])
    $geniexVer = $parts[4]
    $llamaVer = $parts[5]

    Report-Check "GenieX CLI Runtime" $geniexOk "Versione: $geniexVer"
    Report-Check "llama-server Hexagon" $llamaOk "Versione: $llamaVer"
    Report-Check "Allineamento Commit Runtime" $commitAligned "Commit corrispondente"
    Report-Check "Integrita' Binari Runtime" $filesAligned "File e firme Authenticode verificati"
} else {
    Report-Check "Verifica Stack Runtime" $false "stack_manager.py ha fallito l'ispezione"
}

# 5. Modelli GGUF in cache
$ModelsDir = Join-Path $env:USERPROFILE ".cache\geniex\models"
$ModelsExist = Test-Path $ModelsDir
$GgufCount = 0
if ($ModelsExist) {
    $GgufCount = (Get-ChildItem -Path $ModelsDir -Filter "*.gguf" -File).Count
}
Report-Check "Cache Modelli GGUF" ($GgufCount -gt 0) "Trovati $GgufCount file in $ModelsDir"

# 6. Configurazione Kilo Code locale
$KiloJsonPath = Join-Path $ScriptDir "kilo.json"
if (-not (Test-Path $KiloJsonPath) -and -not $CheckOnly) {
    python (Join-Path $ScriptDir "kilo_config.py") --generate | Out-Null
}
$KiloJsonExists = Test-Path $KiloJsonPath
Report-Check "File configurazione kilo.json" $KiloJsonExists "Percorso: $KiloJsonPath"

# 7. Validazione Regole Offline e Sicurezza
$OfflineValid = $false
$OfflineIssues = @()
if ($KiloJsonExists) {
    $valOutput = python (Join-Path $ScriptDir "kilo_config.py") --check 2>&1
    if ($LASTEXITCODE -eq 0) {
        $OfflineValid = $true
    } else {
        $OfflineIssues += $valOutput
    }
}
Report-Check "Sicurezza Endpoint Offline (Loopback 127.0.0.1)" $OfflineValid $(if (-not $OfflineValid) { $OfflineIssues -join "; " })

# 8. Agenti e Subagenti personalizzati
$AgentsDir = Join-Path $ScriptDir ".kilo\agents"
$AgentsExist = Test-Path $AgentsDir
$AgentCount = if ($AgentsExist) { (Get-ChildItem -Path $AgentsDir -Filter "*.md").Count } else { 0 }
Report-Check "Agenti e Subagenti personalizzati (.kilo/agents)" ($AgentCount -ge 4) "Trovati $AgentCount ruoli (code, plan, debug, explore, reviewer)"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " RIEPILOGO: $Passed superati, $Failed falliti, $Warnings avvisi." -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
Write-Host "========================================================" -ForegroundColor Cyan

if ($Failed -gt 0) {
    Write-Host "`nRisolvi gli errori sopra elencati prima di avviare l'agente." -ForegroundColor Yellow
    exit 1
} else {
    Write-Host "`nSistema pronto! Avvia l'agente con: .\launch-kilo.ps1" -ForegroundColor Green
    exit 0
}
