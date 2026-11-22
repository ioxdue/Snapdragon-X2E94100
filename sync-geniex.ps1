<#
.SYNOPSIS
    Installa e allinea automaticamente llama.cpp ARM64 al runtime di GenieX.
.DESCRIPTION
    Senza Apply/AutoConfirm esegue soltanto il controllo. Con Apply prepara e
    verifica una nuova directory, poi sostituisce la destinazione conservando
    il backup. Al primo avvio non richiede llama.cpp, Python, Git o SDK.
#>
param(
    [string]$BinPath = (Join-Path $env:USERPROFILE 'llama-cpp\llama-bin-win-cpu-arm64'),
    [string]$GenieXDir = (Join-Path $env:LOCALAPPDATA 'GenieX CLI'),
    [switch]$Apply,
    [switch]$AutoConfirm,
    [switch]$Force,
    [switch]$NoStopServer,
    [switch]$UseHardLinks,
    [switch]$UpdateGenieX,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    Import-Module (Join-Path $PSScriptRoot 'runtime-update.psm1') -Force
    if ($UpdateGenieX) {
        # Prima separa le copie installate da eventuali hard-link legacy a GenieX.
        [void](Sync-HexagonRuntime -BinPath $BinPath -GenieXDir $GenieXDir -Apply -Force -NoStopServer:$NoStopServer)
        $genie = Get-GenieRuntime $GenieXDir
        [void](Invoke-RuntimeNative $genie.Exe 'update --silent' $genie.Directory 600000)
    }
    $result = Sync-HexagonRuntime -BinPath $BinPath -GenieXDir $GenieXDir -Apply:($Apply -or $AutoConfirm -or $UpdateGenieX) `
        -Force:$Force -NoStopServer:$NoStopServer -UseHardLinks:$UseHardLinks
    if ($Json) { $result | ConvertTo-Json -Depth 6 -Compress } else {
        Write-Host $result.Message -ForegroundColor Green
        if ($result.Backup) { Write-Host "Backup: $($result.Backup)" }
    }
    exit 0
} catch {
    if ($Json) { @{ Error = $_.Exception.Message } | ConvertTo-Json -Compress }
    else { Write-Error $_.Exception.Message -ErrorAction Continue }
    exit 1
}
