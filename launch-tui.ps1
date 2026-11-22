<#
.SYNOPSIS
    Avvia la chat Hexagon Claw oppure, con ControlCenter, il dashboard unificato.
    Prepara Python e dipendenze in un ambiente dedicato; il runtime viene allineato
    soltanto nella modalita' chat legacy, non all'apertura del centro di controllo.
#>
param(
    [string]$Url = 'http://127.0.0.1:18803/v1',
    [string]$Model = '',
    [string]$SystemPrompt = '',
    [string]$Workspace = '',
    [ValidateRange(1, 2147483647)][int]$MaxTokens = 2048,
    [switch]$FromBatch,
    [switch]$ControlCenter,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$TuiArguments
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

try {
    $launcherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($Workspace)) { $Workspace = $launcherRoot }
    $scriptName = if ($ControlCenter) { 'hexagon_app.py' } else { 'tui.py' }
    $scriptPath = Join-Path $launcherRoot $scriptName
    if (-not (Test-Path -LiteralPath $scriptPath)) { throw "$scriptName non trovato in $PSScriptRoot" }
    # La CLI batch conserva la sintassi Python. I flag riconosciuti vengono consumati
    # una sola volta (precedenza alla riga di comando, come l'ultimo-vince di argparse);
    # gli argomenti rimasti vengono inoltrati intatti a Python.
    if ($FromBatch) {
        $known = @{ '--url' = 'Url'; '--workspace' = 'Workspace'; '--model' = 'Model'; '--system' = 'SystemPrompt' }
        $cliFlags = [ordered]@{}
        $remaining = New-Object System.Collections.Generic.List[string]
        for ($i = 0; $i -lt $TuiArguments.Count; $i++) {
            $token = [string]$TuiArguments[$i]
            if ($known.ContainsKey($token) -and $i + 1 -lt $TuiArguments.Count) {
                $cliFlags[$token] = [string]$TuiArguments[$i + 1]; $i++
            } elseif ($token -eq '--max-tokens' -and $i + 1 -lt $TuiArguments.Count) {
                $cliFlags[$token] = [string]$TuiArguments[$i + 1]; $i++
            } else {
                $remaining.Add($token)
            }
        }
        if ($cliFlags.Contains('--url')) { $Url = $cliFlags['--url'] }
        if ($cliFlags.Contains('--workspace')) { $Workspace = $cliFlags['--workspace'] }
        if ($cliFlags.Contains('--model')) { $Model = $cliFlags['--model'] }
        if ($cliFlags.Contains('--system')) { $SystemPrompt = $cliFlags['--system'] }
        if ($cliFlags.Contains('--max-tokens')) {
            $parsed = 0
            if ([int]::TryParse($cliFlags['--max-tokens'], [ref]$parsed)) { $MaxTokens = $parsed }
            else { $remaining.Add('--max-tokens'); $remaining.Add($cliFlags['--max-tokens']) }
        }
        $TuiArguments = $remaining.ToArray()
    }
    if (-not $ControlCenter) {
        $uri = [uri]$Url
        if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @('http', 'https')) { throw 'URL HTTP(S) non valido.' }
        if ($uri.Host -in @('127.0.0.1', 'localhost', '[::1]')) {
            & (Join-Path $Workspace 'sync-geniex.ps1') -Apply
            if ($LASTEXITCODE -ne 0) { throw 'Preparazione automatica runtime fallita.' }
        }
    }
    Import-Module (Join-Path $launcherRoot 'tui-bootstrap.psm1') -Force
    $python = Resolve-HexagonTuiPython -Requirements (Join-Path $launcherRoot 'requirements.txt')
    # Il centro di controllo non prepara il runtime e non avvia server all'apertura.
    $flagValues = if ($ControlCenter) {
        [ordered]@{ '--url' = $Url; '--workspace' = $Workspace }
    } else {
        [ordered]@{
            '--url' = $Url; '--workspace' = $Workspace; '--model' = $Model
            '--system' = $SystemPrompt; '--max-tokens' = [string]$MaxTokens
        }
    }
    $arguments = @($scriptPath)
    foreach ($f in $flagValues.Keys) {
        $value = [string]$flagValues[$f]
        if ($f -in @('--model', '--system') -and [string]::IsNullOrWhiteSpace($value)) { continue }
        $arguments += @($f, $value)
    }
    if (-not $ControlCenter) { $arguments += @($TuiArguments) }
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
