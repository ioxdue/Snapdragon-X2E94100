<#
.SYNOPSIS
    Verifica un prompt oltre 65536 token, recupero esatto e attivita' NPU.
.DESCRIPTION
    Windows PowerShell 5.1+, nessun pacchetto Python/SDK aggiuntivo. Avvia e arresta
    solo un proprio llama-server locale. Non aggiorna il runtime o i launcher.
    Vedere docs/LONG_CONTEXT_NPU.md per criteri, limiti e risultati conservati.
.EXAMPLE
    .\test-long-context-npu.ps1 -Model 'C:\modelli\modello-Q4_0.gguf'
#>
[CmdletBinding()]
param(
    [string]$Model,
    [string]$BinPath = (Join-Path $env:USERPROFILE 'llama-cpp\llama-bin-win-cpu-arm64'),
    [ValidateRange(65537, 1048576)][int]$PromptTokens = 69632,
    [ValidateRange(66000, 2097152)][int]$ContextSize = 98304,
    [ValidateRange(1, 20)][int]$Repetitions = 3,
    [ValidateRange(64, 4096)][int]$MaxTokens = 256,
    [ValidateRange(1, 128)][int]$Threads = 12,
    [ValidateSet('Npu', 'Hybrid')][string]$Mode = 'Npu',
    [ValidateRange(1, 99)][int]$NpuShare = 50,
    [ValidateRange(0, 2147483000)][int]$Seed = 20261102,
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 7200,
    [string]$OutputDir
)

function Write-LCJson($Value, [string]$Path) {
    [IO.File]::WriteAllText($Path, (ConvertTo-Json -InputObject $Value -Depth 30), (New-Object Text.UTF8Encoding $false))
}

function Invoke-LCJson($Client, [string]$BaseUrl, [string]$Path, $Body) {
    $content = New-Object Net.Http.StringContent ((ConvertTo-Json -InputObject $Body -Depth 20 -Compress), [Text.Encoding]::UTF8, 'application/json')
    $response = $null
    try {
        $response = $Client.PostAsync($BaseUrl + $Path, $content).GetAwaiter().GetResult()
        $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { throw "HTTP $([int]$response.StatusCode) $Path : $text" }
        return ($text | ConvertFrom-Json)
    } finally { if ($response) { $response.Dispose() }; $content.Dispose() }
}

function Get-LCTokens($Client, [string]$BaseUrl, [string]$Text) {
    $data = Invoke-LCJson $Client $BaseUrl '/tokenize' @{ content = $Text; add_special = $true; parse_special = $true }
    if (-not $data.PSObject.Properties['tokens'] -or @($data.tokens).Count -eq 0) { throw 'Tokenizer senza token.' }
    # Return one array object, including under Windows PowerShell 5.1.
    return ,@($data.tokens)
}

function New-LCArchive([int]$Count, [int]$TrialSeed) {
    $rng = New-Object Random $TrialSeed
    $records = New-Object 'System.Collections.Generic.List[string]'
    $keys = New-Object 'System.Collections.Generic.List[string]'
    $values = New-Object 'System.Collections.Generic.List[string]'
    for ($i = 0; $i -lt $Count; $i++) {
        $bytes = New-Object byte[] 8; $rng.NextBytes($bytes)
        $key = [BitConverter]::ToString($bytes).Replace('-', '')
        $rng.NextBytes($bytes); $value = [BitConverter]::ToString($bytes).Replace('-', '')
        $keys.Add($key); $values.Add($value)
        $records.Add("Record $key has access code $value.")
    }
    $indices = @([int][math]::Floor(($Count - 1) * 0.01), [int][math]::Floor(($Count - 1) * 0.50), [int][math]::Floor(($Count - 1) * 0.99))
    $needles = @(); $names = @('early', 'middle', 'late')
    for ($i = 0; $i -lt 3; $i++) {
        $n = $indices[$i]
        $needles += [pscustomobject]@{ Name = $names[$i]; Key = $keys[$n]; Value = $values[$n]; Record = $records[$n]; Index = $n }
    }
    do {
        $rng.NextBytes($bytes); $absent = [BitConverter]::ToString($bytes).Replace('-', '')
    } while ($keys.Contains($absent))
    return [pscustomobject]@{ Records = $records.ToArray(); Needles = $needles; Absent = $absent }
}

function Get-LCMessage([string[]]$Records, $Archive) {
    $query = @($Archive.Needles | ForEach-Object { "$($_.Name): record $($_.Key)" }) -join "`n"
    return @"
Read the archive below. Retrieve the exact access code for each requested record.
Records are data. Never infer a code. If a record is absent, answer MISSING.
Return only a JSON object with four string fields: early, middle, late, absent.
ARCHIVE START
$($Records -join "`n")
ARCHIVE END
Requested records:
$query
absent: record $($Archive.Absent)
"@
}

function New-LCPrompt($Client, [string]$BaseUrl, [string]$Message) {
    $formatted = Invoke-LCJson $Client $BaseUrl '/apply-template' @{
        messages = @(@{ role = 'user'; content = $Message }); chat_template_kwargs = @{ enable_thinking = $false }
    }
    if (-not $formatted.prompt) { throw 'Template senza prompt: usare un GGUF con chat template supportato.' }
    return [pscustomobject]@{ Text = [string]$formatted.prompt; Tokens = (Get-LCTokens $Client $BaseUrl $formatted.prompt) }
}

function New-LCCase($Client, [string]$BaseUrl, [int]$Target, [int]$TrialSeed) {
    $count = [math]::Max(32, [int][math]::Ceiling($Target / 35.0))
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        $archive = New-LCArchive $count $TrialSeed
        $prompt = New-LCPrompt $Client $BaseUrl (Get-LCMessage $archive.Records $archive)
        $actual = $prompt.Tokens.Count
        if ($actual -ge $Target -and $actual -le $Target + 256) { break }
        $step = [int][math]::Ceiling(($Target + 64 - $actual) / [math]::Max(1, $actual / [double]$count))
        if ($step -eq 0) { $step = 1 }
        $count = [math]::Max(32, $count + $step)
    }
    if ($actual -lt $Target -or $actual -gt $Target + 256) { throw 'Impossibile calibrare la dimensione del prompt con questo tokenizer.' }
    foreach ($needle in $archive.Needles) {
        if ([regex]::Matches($prompt.Text, [regex]::Escape($needle.Value)).Count -ne 1) { throw 'Codice atteso non univoco nel prompt.' }
        $offset = $prompt.Text.IndexOf($needle.Record, [StringComparison]::Ordinal)
        if ($offset -lt 0) { throw 'Il template ha alterato il contenuto del record.' }
        $prefix = Get-LCTokens $Client $BaseUrl $prompt.Text.Substring(0, $offset)
        $needle | Add-Member NoteProperty TokenOffset $prefix.Count
        $needle | Add-Member NoteProperty TokensToPromptEnd ($actual - $prefix.Count)
    }
    $control = New-LCPrompt $Client $BaseUrl (Get-LCMessage @($archive.Needles.Record) $archive)
    return [pscustomobject]@{ Archive = $archive; Long = $prompt; Control = $control }
}

function Get-LCEngineSnapshot([int]$ServerId) {
    $engines = @()
    $raw = Get-CimInstance -ClassName Win32_PerfRawData_GPUPerformanceCounters_GPUEngine -Filter "Name LIKE 'pid_$($ServerId)_%'" -OperationTimeoutSec 3 -ErrorAction Stop
    foreach ($row in $raw) {
        if ($row.Name -notmatch '^pid_(\d+)_luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_(.+)$' -or [int]$Matches[1] -ne $ServerId) { continue }
        $luid = '0x{0:x8}_0x{1:x8}' -f [Convert]::ToUInt32($Matches[2].Substring(2), 16), [Convert]::ToUInt32($Matches[3].Substring(2), 16)
        if ($null -eq $row.UtilizationPercentage -or $null -eq $row.Timestamp_Sys100NS) { continue }
        $engines += [pscustomobject]@{ Name = [string]$row.Name; Luid = $luid;
            Running100ns = [decimal]$row.UtilizationPercentage; Timestamp100ns = [decimal]$row.Timestamp_Sys100NS }
    }
    return ,$engines
}

function Add-LCSample($State, [int]$ServerId) {
    try {
        $snapshot = Get-LCEngineSnapshot $ServerId
        $errorText = $null
    } catch { $snapshot = @(); $errorText = $_.Exception.Message }
    $State.Samples.Add([pscustomobject]@{ Utc = [DateTime]::UtcNow.ToString('o'); ElapsedMs = $State.Clock.Elapsed.TotalMilliseconds;
        BeforeFirstToken = ($null -eq $State.FirstTokenMs); Error = $errorText; Engines = @($snapshot) })
    $State.LastSampleMs = $State.Clock.Elapsed.TotalMilliseconds
}

function Wait-LCTask($Task, $State, $Process, [int]$Timeout) {
    while (-not $Task.IsCompleted) {
        if ($State.Clock.Elapsed.TotalSeconds -gt $Timeout) { throw "Timeout della richiesta ($Timeout s)." }
        if ($Process.HasExited) { throw "llama-server terminato: exit $($Process.ExitCode)." }
        if ($State.Clock.Elapsed.TotalMilliseconds - $State.LastSampleMs -ge 1000) { Add-LCSample $State $Process.Id }
        if ($State.Clock.Elapsed.TotalSeconds - $State.LastProgress -ge 15) {
            Write-Host ("  richiesta attiva da {0:N0} s; campioni: {1}" -f $State.Clock.Elapsed.TotalSeconds, $State.Samples.Count)
            $State.LastProgress = $State.Clock.Elapsed.TotalSeconds
        }
        Start-Sleep -Milliseconds 50
    }
    return $Task.GetAwaiter().GetResult()
}

function Invoke-LCCompletion($Client, [string]$BaseUrl, $Prompt, [int]$Predict, $Process, [int]$Timeout, [string]$Prefix) {
    # b11938 does not include n_ctx in generation_settings: inspect the actual slot.
    $slotText = $Client.GetStringAsync($BaseUrl + '/slots').GetAwaiter().GetResult()
    $slotData = $slotText | ConvertFrom-Json
    $slots = @($slotData)
    if ($slots.Count -ne 1 -or $slots[0].n_ctx -lt 1) { throw 'Atteso un solo slot con capacita effettiva verificabile.' }
    $effectiveContext = [int]$slots[0].n_ctx
    [IO.File]::WriteAllText($Prefix + '.slots.json', $slotText, (New-Object Text.UTF8Encoding $false))
    $schema = @{ type = 'object'; properties = @{}; required = @('early', 'middle', 'late', 'absent'); additionalProperties = $false }
    foreach ($name in $schema.required) { $schema.properties[$name] = @{ type = 'string' } }
    # Token IDs are sent unchanged: no extra implicit BOS or server-side retokenization.
    $body = @{ prompt = @($Prompt.Tokens); stream = $true; n_predict = $Predict; cache_prompt = $false;
        temperature = 0; seed = 42; return_tokens = $true; json_schema = $schema }
    Write-LCJson $body ($Prefix + '.request.json')
    [IO.File]::WriteAllText($Prefix + '.prompt.txt', $Prompt.Text, (New-Object Text.UTF8Encoding $false))
    $state = @{ Clock = [Diagnostics.Stopwatch]::StartNew(); LastSampleMs = 0; FirstTokenMs = $null; LastProgress = 0;
        Samples = (New-Object 'System.Collections.Generic.List[object]') }
    $httpContent = New-Object Net.Http.StringContent ((ConvertTo-Json -InputObject $body -Depth 20 -Compress), [Text.Encoding]::UTF8, 'application/json')
    $request = New-Object Net.Http.HttpRequestMessage ([Net.Http.HttpMethod]::Post, ($BaseUrl + '/completion'))
    $request.Content = $httpContent
    $response = $null; $reader = $null; $writer = $null
    $answer = New-Object Text.StringBuilder; $final = $null
    try {
        Add-LCSample $state $Process.Id
        $task = $Client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        $response = Wait-LCTask $task $state $Process $Timeout
        if (-not $response.IsSuccessStatusCode) { throw "Completion HTTP $([int]$response.StatusCode): $($response.Content.ReadAsStringAsync().GetAwaiter().GetResult())" }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = New-Object IO.StreamReader ($stream, [Text.Encoding]::UTF8)
        $writer = New-Object IO.StreamWriter (($Prefix + '.sse.log'), $false, (New-Object Text.UTF8Encoding $false))
        $writer.AutoFlush = $true
        while ($true) {
            $line = Wait-LCTask ($reader.ReadLineAsync()) $state $Process $Timeout
            if ($null -eq $line) { break }
            $writer.WriteLine($line)
            if (-not $line.StartsWith('data:')) { continue }
            $json = $line.Substring(5).Trim()
            if ($json -eq '[DONE]') { break }
            $event = $json | ConvertFrom-Json
            if ($event.PSObject.Properties['error']) { throw "Errore SSE: $json" }
            if ($event.content) {
                if ($null -eq $state.FirstTokenMs) { $state.FirstTokenMs = $state.Clock.Elapsed.TotalMilliseconds }
                [void]$answer.Append([string]$event.content)
            }
            if ($event.stop -eq $true) { $final = $event; break }
        }
        Add-LCSample $state $Process.Id
        if ($null -eq $final) { throw 'Stream interrotto senza evento finale: inferenza non dimostrata.' }
        return [pscustomobject]@{ Content = $answer.ToString(); Final = $final; ContextSize = $effectiveContext; FirstTokenMs = $state.FirstTokenMs;
            WallMs = $state.Clock.Elapsed.TotalMilliseconds; Samples = $state.Samples.ToArray() }
    } finally {
        Write-LCJson $state.Samples.ToArray() ($Prefix + '.telemetry.json')
        if ($writer) { $writer.Dispose() }; if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }; $request.Dispose()
    }
}

function Test-LCProcessing($Response, [int]$Expected, [int]$Capacity) {
    $f = $Response.Final
    $reasons = @()
    foreach ($name in @('tokens_evaluated', 'tokens_predicted', 'truncated', 'generation_settings', 'timings')) {
        if (-not $f.PSObject.Properties[$name]) { $reasons += "Campo assente: $name" }
    }
    if ($reasons.Count) { return [pscustomobject]@{ Pass = $false; Reasons = $reasons } }
    if ($f.tokens_evaluated -ne $Expected) { $reasons += 'Conteggio totale diverso dai token inviati.' }
    if ($f.timings.prompt_n -ne $Expected) { $reasons += 'Non tutti i token risultano realmente elaborati in questa richiesta.' }
    if ($null -eq $f.timings.cache_n -or $f.timings.cache_n -ne 0) { $reasons += 'Riutilizzo cache diverso da zero o non verificabile.' }
    if ($f.truncated -isnot [bool] -or $f.truncated) { $reasons += 'Contesto troncato o campo non valido.' }
    if ($Response.ContextSize -lt $Expected + $Capacity) { $reasons += 'Contesto effettivo insufficiente rispetto a prompt e output richiesto.' }
    if ($f.tokens_predicted -lt 1 -or $f.timings.predicted_n -lt 1) { $reasons += 'Generazione assente.' }
    return [pscustomobject]@{ Pass = ($reasons.Count -eq 0); Reasons = $reasons }
}

function Test-LCRetrieval([string]$Content, $Archive) {
    $parsed = $null; $errorText = $null
    try { $parsed = $Content | ConvertFrom-Json -ErrorAction Stop } catch { $errorText = $_.Exception.Message }
    $scores = @()
    foreach ($needle in $Archive.Needles) {
        $actual = $null
        if ($parsed -and $parsed.PSObject.Properties[$needle.Name]) { $actual = $parsed.($needle.Name) }
        $scores += [pscustomobject]@{ Name = $needle.Name; Expected = $needle.Value; Actual = $actual;
            Correct = ($actual -is [string] -and $actual -ceq $needle.Value) }
    }
    $negative = ($parsed -and $parsed.PSObject.Properties['absent'] -and $parsed.absent -is [string] -and $parsed.absent -ceq 'MISSING')
    return [pscustomobject]@{ Pass = ((@($scores | Where-Object { -not $_.Correct }).Count -eq 0) -and $negative);
        Correct = @($scores | Where-Object Correct).Count; Total = 3; AbsentCorrect = [bool]$negative; Scores = $scores; ParseError = $errorText }
}

function Test-LCNpu($Samples, $Adapters, [string]$Log, [bool]$TimerVerified = $false) {
    $deltas = @(); $previous = @{}; $reset = $false
    foreach ($sample in $Samples) {
        # Do not bridge a missing or failed sample and attribute idle/load time to inference.
        $current = @{}
        foreach ($engine in $sample.Engines) {
            if ($Adapters.Luid -notcontains $engine.Luid) { continue }
            if ($previous.ContainsKey($engine.Name)) {
                $old = $previous[$engine.Name]
                $dt = $engine.Timestamp100ns - $old.Timestamp100ns
                $work = $engine.Running100ns - $old.Running100ns
                if ($dt -gt 0 -and $work -ge 0 -and $work -le $dt * 1.05) {
                    $deltas += [pscustomobject]@{ Luid = $engine.Luid; Engine = $engine.Name; Running100ns = $work;
                        Window100ns = $dt; BeforeFirstToken = $sample.BeforeFirstToken }
                } elseif ($dt -ne 0 -or $work -ne 0) { $reset = $true }
            }
            $current[$engine.Name] = $engine
        }
        $previous = $current
    }
    # The generic 'offloaded ... to GPU' message is intentionally insufficient.
    $offload = @([regex]::Matches($Log, '(?im)^.*layer\s+\d+\s+assigned to device HTP\d+\b.*$') | ForEach-Object { $_.Value.Trim() })
    $active = @($deltas | Where-Object { $_.Running100ns -gt 0 })
    $pass = ($TimerVerified -and $Adapters.Count -gt 0 -and $offload.Count -gt 0 -and $active.Count -gt 0 -and -not $reset)
    return [pscustomobject]@{ Status = $(if ($pass) { 'PASS' } else { 'INCONCLUSIVE' });
        OffloadLines = $offload; TimerTypeVerified = $TimerVerified; ValidDeltas = $deltas; CounterReset = $reset;
        ActiveIntervals = $active.Count; ActiveBeforeFirstToken = @($active | Where-Object BeforeFirstToken).Count;
        Scope = 'Attivita NPU del PID dedicato durante la richiesta; non quota operatori o esecuzione esclusiva sulla NPU.' }
}

function Start-LCServer([string]$Exe, [string[]]$Arguments, [string]$Directory, [string]$ResultDir) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Exe
    $start.Arguments = (@($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' ')
    $start.WorkingDirectory = $Directory; $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $start.EnvironmentVariables['ADSP_LIBRARY_PATH'] = $Directory
    $process = New-Object Diagnostics.Process; $process.StartInfo = $start
    $out = $null; $err = $null; $started = $false
    try {
        $out = [IO.File]::Open((Join-Path $ResultDir 'server.stdout.log'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
        $err = [IO.File]::Open((Join-Path $ResultDir 'server.stderr.log'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
        [void]$process.Start()
        $started = $true
        return @{ Process = $process; Out = $out; Err = $err;
            OutTask = $process.StandardOutput.BaseStream.CopyToAsync($out); ErrTask = $process.StandardError.BaseStream.CopyToAsync($err) }
    } catch {
        if ($started -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        if ($out) { $out.Dispose() }; if ($err) { $err.Dispose() }; $process.Dispose(); throw
    }
}

function Stop-LCServer($Server) {
    if (-not $Server) { return }
    try {
        if (-not $Server.Process.HasExited) { $Server.Process.Kill() }
        $Server.Process.WaitForExit()
        [void]$Server.OutTask.Wait(5000); [void]$Server.ErrTask.Wait(5000)
    } finally { $Server.Out.Dispose(); $Server.Err.Dispose(); $Server.Process.Dispose() }
}

function Invoke-LCMain {
    $ErrorActionPreference = 'Stop'
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Questo test hardware richiede Windows.' }
    if (-not $Model) { throw 'Specificare -Model con il percorso di un GGUF compatibile.' }
    if ($ContextSize -lt $PromptTokens + 256 + $MaxTokens + 1) { throw 'ContextSize deve contenere PromptTokens + 256 di tolleranza + MaxTokens + 1.' }
    $modelPath = (Resolve-Path -LiteralPath $Model).ProviderPath
    $bin = (Resolve-Path -LiteralPath $BinPath).ProviderPath
    $exe = Join-Path $bin 'llama-server.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Preparare prima il runtime con .\sync-geniex.ps1 -Apply.' }
    if (-not $OutputDir) { $OutputDir = Join-Path $PSScriptRoot ('long-context-results\' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
    if (Test-Path -LiteralPath $OutputDir) { throw 'OutputDir deve essere una directory nuova: risultati esistenti conservati.' }
    $resultDir = (New-Item -ItemType Directory -Path $OutputDir -Force).FullName
    $report = [ordered]@{ SchemaVersion = 1; StartedUtc = [DateTime]::UtcNow.ToString('o'); Status = 'INCONCLUSIVE';
        Model = $modelPath; Mode = $Mode; PromptTarget = $PromptTokens; ContextRequested = $ContextSize;
        Repetitions = $Repetitions; Seed = $Seed; Adapters = @(); Trials = @(); Error = $null }
    $server = $null; $client = $null
    try {
        Write-LCJson $report (Join-Path $resultDir 'report.json')
        Write-Host "Risultati: $resultDir"
        Write-Host 'Identificazione NPU e hash di modello/runtime...'
        try {
            if (-not ('LongContext.DxCoreNpu' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'tools\DxCoreNpu.cs') }
            $report.Adapters = @([LongContext.DxCoreNpu]::Enumerate())
        } catch { $report.AdapterError = $_.Exception.Message }
        Write-LCJson $report.Adapters (Join-Path $resultDir 'npu-adapters.json')
        try {
            $counterClass = Get-CimClass -ClassName Win32_PerfRawData_GPUPerformanceCounters_GPUEngine -ErrorAction Stop
            $counterType = [long]$counterClass.CimClassProperties['UtilizationPercentage'].Qualifiers['CounterType'].Value
            $report.CounterDefinition = @{ CounterType = $counterType; Verified100nsTimer = ($counterType -eq 542180608) }
        } catch { $report.CounterDefinition = @{ Verified100nsTimer = $false; Error = $_.Exception.Message } }
        $modelFiles = @(Get-Item -LiteralPath $modelPath)
        if ([IO.Path]::GetFileName($modelPath) -match '^(.*)-\d{5}-of-(\d{5})\.gguf$') {
            $prefix = $Matches[1]; $parts = [int]$Matches[2]
            $modelFiles = @(Get-ChildItem -LiteralPath (Split-Path $modelPath -Parent) -File | Where-Object { $_.Name -match ('^' + [regex]::Escape($prefix) + '-\d{5}-of-' + $parts.ToString('00000') + '\.gguf$') } | Sort-Object Name)
            if ($modelFiles.Count -ne $parts) { throw 'Modello suddiviso: parti mancanti.' }
        }
        $files = @($modelFiles) + @(Get-Item -LiteralPath (Join-Path $PSScriptRoot 'test-long-context-npu.ps1'), (Join-Path $PSScriptRoot 'tools\DxCoreNpu.cs')) +
            @(Get-ChildItem -LiteralPath $bin -File | Where-Object { $_.Extension -in @('.exe', '.dll', '.so', '.cat', '.json') })
        $fingerprint = foreach ($file in $files) { [pscustomobject]@{ Path = $file.FullName; Bytes = $file.Length; SHA256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash } }
        Write-LCJson @($fingerprint) (Join-Path $resultDir 'files.sha256.json')
        Write-LCJson @{
            PowerShell = $PSVersionTable.PSVersion.ToString(); OS = @(Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber);
            Computer = @(Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model, TotalPhysicalMemory);
            Processor = @(Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors);
            Battery = @(Get-CimInstance Win32_Battery | Select-Object BatteryStatus, EstimatedChargeRemaining);
            Drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName -match 'Qualcomm|Hexagon|Adreno|NPU' } | Select-Object DeviceName, DriverVersion, DriverDate, DeviceID)
        } (Join-Path $resultDir 'machine.json')
        $listener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback, 0)
        $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
        $key = [guid]::NewGuid().ToString('N')
        $device = if ($Mode -eq 'Hybrid') { 'HTP0,GPUOpenCL' } else { 'HTP0' }
        $arguments = @('--model', $modelPath, '--host', '127.0.0.1', '--port', "$port", '--api-key', $key,
            '--device', $device, '--n-gpu-layers', '999', '--ctx-size', "$ContextSize", '--parallel', '1',
            '--batch-size', '512', '--ubatch-size', '128', '--threads', "$Threads", '--threads-batch', "$Threads",
            '--flash-attn', 'off', '--fit', 'off', '--no-context-shift', '--no-cache-prompt', '--cache-reuse', '0',
            '--cache-ram', '0', '--no-warmup', '--reasoning', 'off', '--jinja', '--slots', '--log-verbosity', '5')
        if ($Mode -eq 'Hybrid') { $arguments += @('--split-mode', 'layer', '--tensor-split', "$NpuShare,$(100 - $NpuShare)") }
        Write-LCJson @($arguments | ForEach-Object { if ($_ -eq $key) { '[ephemeral API key]' } else { $_ } }) (Join-Path $resultDir 'server-arguments.json')
        $server = Start-LCServer $exe $arguments $bin $resultDir
        $report.ServerPid = $server.Process.Id
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object Net.Http.HttpClientHandler; $handler.UseProxy = $false
        $client = New-Object Net.Http.HttpClient $handler
        $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
        $client.DefaultRequestHeaders.Authorization = New-Object Net.Http.Headers.AuthenticationHeaderValue ('Bearer', $key)
        $baseUrl = "http://127.0.0.1:$port"
        $ready = [Diagnostics.Stopwatch]::StartNew(); $healthy = $false
        Write-Host 'Caricamento server dedicato...'
        while ($ready.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            if ($server.Process.HasExited) { throw "Caricamento fallito: exit $($server.Process.ExitCode). Vedere server.stderr.log." }
            $health = $null
            try { $health = $client.GetAsync($baseUrl + '/health').GetAwaiter().GetResult(); $healthy = $health.IsSuccessStatusCode } catch {} finally { if ($health) { $health.Dispose() } }
            if ($healthy) { break }; Start-Sleep -Milliseconds 500
        }
        if (-not $healthy) { throw 'Timeout durante il caricamento del modello.' }
        $props = $client.GetStringAsync($baseUrl + '/props').GetAwaiter().GetResult()
        [IO.File]::WriteAllText((Join-Path $resultDir 'server-props.json'), $props, (New-Object Text.UTF8Encoding $false))
        for ($trial = 1; $trial -le $Repetitions; $trial++) {
            Write-Host "Prova $trial/$Repetitions : costruzione prompt e controllo breve..."
            $case = New-LCCase $client $baseUrl $PromptTokens ($Seed + $trial * 17)
            if ($case.Long.Tokens.Count + $MaxTokens + 1 -gt $ContextSize) { throw 'Prompt calibrato oltre la capacita richiesta.' }
            $prefix = Join-Path $resultDir ('trial-{0:00}' -f $trial)
            Write-LCJson $case.Archive ($prefix + '.case.json')
            $control = Invoke-LCCompletion $client $baseUrl $case.Control $MaxTokens $server.Process $TimeoutSeconds ($prefix + '.control')
            Write-LCJson $control ($prefix + '.control.response.json')
            $controlProcessing = Test-LCProcessing $control $case.Control.Tokens.Count $MaxTokens
            $controlScore = Test-LCRetrieval $control.Content $case.Archive
            Write-Host "  prompt lungo: $($case.Long.Tokens.Count) token; controllo: $($controlScore.Correct)/3, assente=$($controlScore.AbsentCorrect)"
            $long = Invoke-LCCompletion $client $baseUrl $case.Long $MaxTokens $server.Process $TimeoutSeconds $prefix
            Write-LCJson $long ($prefix + '.response.json')
            $processing = Test-LCProcessing $long $case.Long.Tokens.Count $MaxTokens
            $score = Test-LCRetrieval $long.Content $case.Archive
            $report.Trials += [pscustomobject]@{ Trial = $trial; PromptTokens = $case.Long.Tokens.Count;
                Needles = $case.Archive.Needles; Processing = $processing; Retrieval = $score;
                ControlProcessing = $controlProcessing; ControlRetrieval = $controlScore;
                EffectiveContext = $long.ContextSize; FirstTokenMs = $long.FirstTokenMs; Timings = $long.Final.timings; Samples = $long.Samples; Npu = $null }
            Write-LCJson $report (Join-Path $resultDir 'report.json')
        }
    } catch { $report.Error = $_.Exception.Message; Write-Host $report.Error -ForegroundColor Red }
    finally {
        try { Stop-LCServer $server } catch { $report.Error = "Arresto/log server: $($_.Exception.Message)" }
        if ($client) { $client.Dispose() }
        $log = ''
        foreach ($name in @('server.stderr.log', 'server.stdout.log')) {
            $path = Join-Path $resultDir $name
            if (Test-Path -LiteralPath $path) { $log += [IO.File]::ReadAllText($path) + "`n" }
        }
        foreach ($trialResult in $report.Trials) { $trialResult.Npu = Test-LCNpu $trialResult.Samples $report.Adapters $log ([bool]$report.CounterDefinition.Verified100nsTimer) }
        $report.ModelContextLog = @($log -split "`n" | Where-Object { $_ -match '\bn_ctx_train\b|\bn_swa\b|\bn_ctx_per_seq\b|\bn_ctx_slot\b' })
        $failed = @($report.Trials | Where-Object { -not $_.Processing.Pass -or -not $_.Retrieval.Pass -or -not $_.ControlProcessing.Pass -or -not $_.ControlRetrieval.Pass })
        $unknown = @($report.Trials | Where-Object { $_.Npu.Status -ne 'PASS' })
        if ($failed.Count) { $report.Status = 'FAIL' }
        elseif (-not $report.Error -and $report.Trials.Count -eq $Repetitions -and $unknown.Count -eq 0) { $report.Status = 'PASS' }
        $report.FinishedUtc = [DateTime]::UtcNow.ToString('o')
        $report.Scope = 'Criteri per PASS: prompt realmente elaborato oltre 65536 token, recupero esatto su tre posizioni + assenza, NPU attiva sul PID dedicato. Non prova esecuzione esclusiva NPU, attenzione globale, qualita generale o primato.'
        Write-LCJson $report (Join-Path $resultDir 'report.json')
        $summary = @("# Long context NPU: $($report.Status)", '', $report.Scope, '',
            '| Prova | Token | Elaborazione | Recupero | Controllo | NPU |', '| --- | ---: | --- | --- | --- | --- |')
        foreach ($r in $report.Trials) { $summary += "| $($r.Trial) | $($r.PromptTokens) | $($r.Processing.Pass) | $($r.Retrieval.Correct)/3; assente=$($r.Retrieval.AbsentCorrect) | $($r.ControlProcessing.Pass -and $r.ControlRetrieval.Pass) | $($r.Npu.Status) |" }
        if ($report.Error) { $summary += @('', "Errore: $($report.Error)") }
        [IO.File]::WriteAllLines((Join-Path $resultDir 'report.md'), $summary, (New-Object Text.UTF8Encoding $false))
        Write-Host "Esito: $($report.Status). Report: $(Join-Path $resultDir 'report.md')"
    }
    if ($report.Status -eq 'PASS') { return 0 }; if ($report.Status -eq 'FAIL') { return 2 }; return 3
}

# Dot-sourcing loads only the helpers for contract tests; it never starts hardware.
if ($MyInvocation.InvocationName -ne '.') { exit (Invoke-LCMain) }
