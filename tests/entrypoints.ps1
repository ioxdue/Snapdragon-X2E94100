# Test dei wrapper veri in processi separati. Gli EXE fixture simulano version/device,
# senza modelli, driver o processi NPU reali. PowerShell 5.1 fornisce il compilatore C#.
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { Write-Host 'ENTRYPOINT_TESTS=SKIP (Windows wrappers)'; exit 0 }
$root = Join-Path ([IO.Path]::GetTempPath()) ('hexagon-entry-tests-' + [guid]::NewGuid().ToString('N'))
$repo = Split-Path $PSScriptRoot -Parent
$checkout = Join-Path $root 'checkout with spaces'
$profile = Join-Path $root 'user profile'
$appdata = Join-Path $root 'local appdata'
$genie = Join-Path $appdata 'GenieX CLI'
$runtime = Join-Path $genie 'llama_cpp'
$bin = Join-Path $profile 'llama-cpp\llama-bin-win-cpu-arm64'
$models = Join-Path $profile '.cache\geniex\models'
$savedProfile = $env:USERPROFILE; $savedAppData = $env:LOCALAPPDATA
$savedMarker = $env:HEXAGON_TEST_MARKER; $savedPython = $env:HEXAGON_TEST_PYTHON
$savedExit = $env:HEXAGON_TEST_EXIT; $savedFixture = $env:HEXAGON_TEST_FIXTURE
$shell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
foreach ($path in @($checkout,$runtime,$models)) { [void][IO.Directory]::CreateDirectory($path) }
function Run-Entry([string]$Arguments) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $shell; $start.Arguments = $Arguments
    $start.UseShellExecute = $false; $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) { $process.Kill(); throw 'Entry point timed out' }
        $process.WaitForExit()
        return @{ Code=$process.ExitCode; Output=$stdout.Result; Error=$stderr.Result }
    } finally { $process.Dispose() }
}
try {
    foreach ($name in @('launch-hexagon.ps1','sync-geniex.ps1','runtime-update.psm1','launch-tui.ps1')) {
        Copy-Item -LiteralPath (Join-Path $repo $name) -Destination $checkout
    }
    $fixture = Join-Path $root 'fixture.exe'
    Add-Type -TypeDefinition @'
using System;
using System.IO;
public class HexagonExecutableFixture {
    public static int Main(string[] args) {
        if (Array.IndexOf(args, "--version") >= 0) {
            Console.WriteLine("GenieX CLI Version: 0.10.0\nLlamaCPP Runtime Hash: 19cce6919\nversion: 11938 (19cce6919)");
        } else if (Array.IndexOf(args, "--list-devices") >= 0) {
            Console.WriteLine("HTP0 GPUOpenCL");
        } else {
            File.WriteAllText(Environment.GetEnvironmentVariable("HEXAGON_TEST_MARKER"), String.Join("\n", args));
            if (Environment.GetEnvironmentVariable("HEXAGON_TEST_EXIT") == "7") return 7;
        }
        return 0;
    }
}
'@ -OutputAssembly $fixture -OutputType ConsoleApplication
    Copy-Item -LiteralPath $fixture -Destination (Join-Path $genie 'geniex.exe')
    foreach ($name in @('ggml.dll','ggml-hexagon.dll','ggml-opencl.dll','libggml-htp-v81.so','libggml-htp.cat')) {
        [IO.File]::WriteAllText((Join-Path $runtime $name), 'fixture-' + $name)
    }
    # Stub SOLO dei confini esterni; la transazione e il wrapper sono quelli di produzione.
    $overrides = @'
function Get-LlamaReleaseAsset($Commit) { return @{ Commit=$Commit } }
function Get-RuntimeArchive($Asset, $Destination) { [IO.File]::WriteAllText($Destination, 'fixture') }
function Expand-RuntimeArchive($Archive,$Destination) {
    foreach($name in @('llama-server.exe','llama-cli.exe','llama-bench.exe')) { Copy-Item -LiteralPath $env:HEXAGON_TEST_FIXTURE -Destination (Join-Path $Destination $name) }
}
function Assert-Arm64Executable($Path) { } # L'architettura e' provata separatamente con fixture PE in runtime-update.ps1.
function Get-RuntimeProcesses($Directory) { return @() }
'@
    Add-Content -LiteralPath (Join-Path $checkout 'runtime-update.psm1') -Value $overrides
    $env:HEXAGON_TEST_FIXTURE = $fixture
    $env:USERPROFILE = $profile; $env:LOCALAPPDATA = $appdata
    $env:HEXAGON_TEST_MARKER = Join-Path $root 'server-started.txt'
    # GGUF minimo ma con teste/KV e contesto validi per il launcher.
    $writer = New-Object IO.BinaryWriter([IO.File]::Create((Join-Path $models 'fixture.gguf')))
    try {
        function Write-String($Writer, [string]$Value) {
            $data = [Text.Encoding]::UTF8.GetBytes($Value); $Writer.Write([uint64]$data.Length); $Writer.Write($data)
        }
        $writer.Write([Text.Encoding]::ASCII.GetBytes('GGUF')); $writer.Write([uint32]3)
        $writer.Write([uint64]0); $writer.Write([uint64]7)
        Write-String $writer 'general.architecture'; $writer.Write([uint32]8); Write-String $writer 'llama'
        foreach ($entry in @(@('general.file_type',2),@('llama.context_length',8192),@('llama.embedding_length',4096),
                             @('llama.block_count',32),@('llama.attention.head_count',32),@('llama.attention.head_count_kv',8))) {
            Write-String $writer $entry[0]; $writer.Write([uint32]4); $writer.Write([uint32]$entry[1])
        }
    } finally { $writer.Dispose() }
    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-hexagon.ps1') + '" -ListOnly -Json')
    $inventory = $result.Output | ConvertFrom-Json
    if ($result.Code -ne 0 -or @($inventory.models).Count -ne 1) { throw "JSON inventory failed: $($result.Output) $($result.Error)" }
    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-hexagon.ps1') + '" -Mode Router -DryRun -Json')
    $plan = $result.Output | ConvertFrom-Json
    if ($result.Code -ne 0 -or $plan.models[0].alias -ne 'fixture') { throw "JSON preview failed: $($result.Output) $($result.Error)" }
    if (Test-Path (Join-Path $checkout 'hexagon-models.ini')) { throw 'JSON preview wrote the INI' }
    if (Test-Path $env:HEXAGON_TEST_MARKER) { throw 'JSON preview started the server' }
    Write-Host 'PASS JSON inventory and router preview have no filesystem or process side effects'

    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-hexagon.ps1') + '" -Mode Router')
    if ($result.Code -ne 0 -or -not (Test-Path $env:HEXAGON_TEST_MARKER)) { throw "First launch failed: $($result.Output) $($result.Error)" }
    if (-not (Test-Path (Join-Path $bin 'hexagon-runtime.json'))) { throw 'First launch skipped installation' }
    Write-Host 'PASS real launcher first boot with absent llama.cpp directory'

    $env:HEXAGON_TEST_EXIT = '7'
    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-hexagon.ps1') + '" -Model fixture.gguf')
    if ($result.Code -ne 7) { throw "Direct server exit code lost: $($result.Code) $($result.Output) $($result.Error)" }
    $env:HEXAGON_TEST_EXIT = $savedExit
    Write-Host 'PASS direct launcher propagates server exit code'

    # La TUI usa il bootstrap, conserva flag e spazi e restituisce il codice Python.
    $env:HEXAGON_TEST_PYTHON = (Get-Command python.exe -ErrorAction Stop).Source
    [IO.File]::WriteAllText((Join-Path $checkout 'tui-bootstrap.psm1'), 'function Resolve-HexagonTuiPython { param($Requirements) return $env:HEXAGON_TEST_PYTHON }; Export-ModuleMember -Function Resolve-HexagonTuiPython')
    [IO.File]::WriteAllText((Join-Path $checkout 'requirements.txt'), '')
    [IO.File]::WriteAllText((Join-Path $checkout 'tui.py'), 'import sys,json; print(json.dumps(sys.argv)); sys.exit(7)')
    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-tui.ps1') + '" -FromBatch --url http://127.0.0.1:18803/v1 --model "Case With Spaces" --max-tokens 32')
    if ($result.Code -ne 7) { throw "Python exit code lost: $($result.Code) $($result.Output) $($result.Error)" }
    $lastLine = ($result.Output.Trim() -split '\r?\n')[-1]
    $arguments = $lastLine | ConvertFrom-Json
    if ($arguments[$arguments.IndexOf('--model')+1] -ne 'Case With Spaces' -or $arguments[-1] -ne '32') { throw "CLI arguments lost: $lastLine" }
    Write-Host 'PASS TUI launcher reuses bootstrap, preserves CLI arguments and exit code'

    [IO.File]::WriteAllText((Join-Path $checkout 'hexagon_app.py'), 'import sys,json; print(json.dumps(sys.argv)); sys.exit(9)')
    Remove-Item -LiteralPath $bin -Recurse -Force
    $result = Run-Entry ('-NoProfile -File "' + (Join-Path $checkout 'launch-tui.ps1') + '" -ControlCenter')
    if ($result.Code -ne 9) { throw "Control center exit code lost: $($result.Code) $($result.Output) $($result.Error)" }
    if (Test-Path (Join-Path $bin 'hexagon-runtime.json')) { throw 'Control center bootstrap changed the runtime' }
    $lastLine = ($result.Output.Trim() -split '\r?\n')[-1]
    $arguments = $lastLine | ConvertFrom-Json
    if ($arguments[0] -notlike '*hexagon_app.py' -or $arguments -contains '--model') { throw "Control center arguments invalid: $lastLine" }
    Write-Host 'PASS control center bootstrap skips runtime sync and preserves Python exit code'

    Copy-Item -LiteralPath (Join-Path $repo 'hexagon.bat') -Destination $checkout
    $batch = Join-Path $checkout 'hexagon.bat'
    $result = Run-Entry ('-NoProfile -Command "& ''' + $batch + '''"; exit $LASTEXITCODE')
    if ($result.Code -ne 9 -or $result.Output -notmatch 'hexagon_app.py') { throw "hexagon.bat failed: $($result.Code) $($result.Output) $($result.Error)" }
    Write-Host 'PASS hexagon.bat is parameterless and preserves the control center exit code'
    Write-Host 'ENTRYPOINT_TESTS=PASS (6 Windows process scenarios)'
} finally {
    $env:USERPROFILE=$savedProfile; $env:LOCALAPPDATA=$savedAppData
    $env:HEXAGON_TEST_MARKER=$savedMarker; $env:HEXAGON_TEST_PYTHON=$savedPython
    $env:HEXAGON_TEST_FIXTURE=$savedFixture; $env:HEXAGON_TEST_EXIT=$savedExit
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
