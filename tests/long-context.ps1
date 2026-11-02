$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'test-long-context-npu.ps1') -BinPath $PSScriptRoot
$script:checks = 0
function Assert-LC([bool]$Condition, [string]$Message) {
    $script:checks++
    if (-not $Condition) { throw $Message }
}

# Compile the actual interop declarations; no dxcore.dll calls off Windows.
if (-not ('LongContext.DxCoreNpu' -as [type])) { Add-Type -Path (Join-Path $root 'tools/DxCoreNpu.cs') }

$archive = New-LCArchive 2500 12345
$again = New-LCArchive 2500 12345
$other = New-LCArchive 2500 12346
Assert-LC (($archive.Records -join "`n") -ceq ($again.Records -join "`n")) 'Seed does not reproduce the corpus'
Assert-LC (($archive.Records -join "`n") -cne ($other.Records -join "`n")) 'Trials repeat the same corpus'
Assert-LC ($archive.Needles.Count -eq 3 -and $archive.Needles[0].Index -lt 30 -and $archive.Needles[2].Index -gt 2400) 'Missing early/middle/late needles'
$message = Get-LCMessage $archive.Records $archive
$question = $message.Substring($message.IndexOf('Requested records:'))
foreach ($needle in $archive.Needles) {
    Assert-LC (-not $question.Contains($needle.Value)) 'Answer leaked into the question'
    Assert-LC ($question.Contains($needle.Key)) 'Question is missing a key'
}
Assert-LC (-not (($archive.Records -join "`n").Contains($archive.Absent))) 'Absent key occurs in the corpus'
$answer = @{ absent = 'MISSING' }
foreach ($needle in $archive.Needles) { $answer[$needle.Name] = $needle.Value }
$good = ConvertTo-Json $answer
Assert-LC ((Test-LCRetrieval $good $archive).Pass) 'Exact answers rejected'
$answer.early = $answer.early.ToLowerInvariant()
Assert-LC (-not (Test-LCRetrieval (ConvertTo-Json $answer) $archive).Pass) 'Inexact code accepted'
$answer.early = $archive.Needles[0].Value; $answer.absent = $answer.late
Assert-LC (-not (Test-LCRetrieval (ConvertTo-Json $answer) $archive).Pass) 'Hallucinated absent value accepted'
Assert-LC (-not (Test-LCRetrieval 'not JSON' $archive).Pass) 'Invalid answer accepted'
$answer.absent = @('MISSING')
Assert-LC (-not (Test-LCRetrieval (ConvertTo-Json $answer) $archive).Pass) 'Non-string absence accepted'

function New-CompletionFixture {
    return [pscustomobject]@{ ContextSize = 98304; Final = [pscustomobject]@{
        tokens_evaluated = 69632; tokens_predicted = 80; truncated = $false;
        tokens_cached = 69711; # Resident KV tokens, NOT the count reused from an earlier prompt.
        generation_settings = [pscustomobject]@{ temperature = 0 };
        timings = [pscustomobject]@{ prompt_n = 69632; cache_n = 0; predicted_n = 80 }
    } }
}
Assert-LC ((Test-LCProcessing (New-CompletionFixture) 69632 256).Pass) 'Matching full prompt rejected, or tokens_cached misinterpreted'
foreach ($case in @('short-prompt', 'cached', 'truncated', 'context', 'no-generation', 'missing-cache')) {
    $response = New-CompletionFixture
    switch ($case) {
        'short-prompt' { $response.Final.timings.prompt_n = 4096 }
        'cached' { $response.Final.timings.cache_n = 65536 }
        'truncated' { $response.Final.truncated = $true }
        'context' { $response.ContextSize = 65536 }
        'no-generation' { $response.Final.tokens_predicted = 0 }
        'missing-cache' { $response.Final.timings.PSObject.Properties.Remove('cache_n') }
    }
    Assert-LC (-not (Test-LCProcessing $response 69632 256).Pass) "Invalid completion accepted: $case"
}

$script:rawRows = @(
    [pscustomobject]@{ Name = 'pid_123_luid_0x0_0x1_phys_0_eng_0_engtype_Compute'; UtilizationPercentage = 100; Timestamp_Sys100NS = 1000 },
    [pscustomobject]@{ Name = 'pid_1234_luid_0x0_0x1_phys_0_eng_0_engtype_Compute'; UtilizationPercentage = 999; Timestamp_Sys100NS = 1000 }
)
function Get-CimInstance { param($ClassName, $Filter, $OperationTimeoutSec, $ErrorAction) return $script:rawRows }
$snapshot = Get-LCEngineSnapshot 123
Assert-LC ($snapshot.Count -eq 1 -and $snapshot[0].Luid -eq '0x00000000_0x00000001') 'Wrong PID or LUID normalization'
$adapters = @([pscustomobject]@{ Luid = '0x00000000_0x00000001' })
function New-Sample([decimal]$Running, [decimal]$Time, [string]$Luid = '0x00000000_0x00000001') {
    return [pscustomobject]@{ BeforeFirstToken = $true; Engines = @([pscustomobject]@{
        Name = 'pid_123_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_Compute'; Luid = $Luid;
        Running100ns = $Running; Timestamp100ns = $Time
    }) }
}
$log = 'load_tensors: layer 0 assigned to device HTP0, is_swa = 0'
$active = @((New-Sample 100 1000), (New-Sample 300 2000))
Assert-LC ((Test-LCNpu $active $adapters $log $true).Status -eq 'PASS') 'PID-specific activity and explicit HTP placement rejected'
Assert-LC ((Test-LCNpu $active $adapters $log).Status -eq 'INCONCLUSIVE') 'Unknown counter semantics accepted'
Assert-LC ((Test-LCNpu $active $adapters 'offloaded 25/25 layers to GPU' $true).Status -eq 'INCONCLUSIVE') 'Generic GPU log treated as NPU proof'
Assert-LC ((Test-LCNpu $active @() $log $true).Status -eq 'INCONCLUSIVE') 'Unknown hardware identity accepted'
$gpu = @((New-Sample 100 1000 '0x00000000_0x00000002'), (New-Sample 300 2000 '0x00000000_0x00000002'))
Assert-LC ((Test-LCNpu $gpu $adapters $log $true).Status -eq 'INCONCLUSIVE') 'Adreno Compute counter treated as NPU'
$idle = @((New-Sample 100 1000), (New-Sample 100 2000))
Assert-LC ((Test-LCNpu $idle $adapters $log $true).Status -eq 'INCONCLUSIVE') 'Idle NPU accepted'
$reset = @((New-Sample 100 1000), (New-Sample 50 2000), (New-Sample 300 3000))
Assert-LC ((Test-LCNpu $reset $adapters $log $true).Status -eq 'INCONCLUSIVE') 'Counter reset accepted'
$gap = @((New-Sample 100 1000), [pscustomobject]@{ BeforeFirstToken = $true; Engines = @() }, (New-Sample 300 3000))
Assert-LC ((Test-LCNpu $gap $adapters $log $true).Status -eq 'INCONCLUSIVE') 'Missing sample bridged with invented activity'
$duplicate = @((New-Sample 100 1000), (New-Sample 100 1000), (New-Sample 300 2000))
Assert-LC ((Test-LCNpu $duplicate $adapters $log $true).Status -eq 'PASS') 'Identical provider snapshot mistaken for a reset'

# Actual child process: exercise stdout/stderr draining, environment isolation and
# cleanup independently of Windows hardware or the fake HTTP server.
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('longctx-process-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $temporary)
$child = $null; $adspBefore = $env:ADSP_LIBRARY_PATH
try {
    $hostExe = @('pwsh', 'pwsh.exe', 'powershell.exe' | ForEach-Object { Join-Path $PSHOME $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })[0]
    $command = "[Console]::Out.WriteLine('fixture stdout'); [Console]::Error.WriteLine('fixture stderr'); Start-Sleep -Seconds 30"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $child = Start-LCServer $hostExe @('-NoLogo', '-NoProfile', '-EncodedCommand', $encoded) $temporary $temporary
    # Let the child initialize and emit both streams, then stop it while still alive.
    Start-Sleep -Seconds 2
    Assert-LC (-not $child.Process.HasExited) 'Child process unexpectedly exited'
    $stopClock = [Diagnostics.Stopwatch]::StartNew()
    Stop-LCServer $child
    Assert-LC ($stopClock.Elapsed.TotalSeconds -lt 10 -and $child.OutTask.IsCompleted -and $child.ErrTask.IsCompleted) 'Child cleanup did not stop and drain its streams'
    $child = $null
    Assert-LC ([IO.File]::ReadAllText((Join-Path $temporary 'server.stdout.log')).Contains('fixture stdout')) 'stdout lost or deadlocked'
    Assert-LC ([IO.File]::ReadAllText((Join-Path $temporary 'server.stderr.log')).Contains('fixture stderr')) 'stderr lost or deadlocked'
    Assert-LC ($env:ADSP_LIBRARY_PATH -ceq $adspBefore) 'Parent ADSP environment changed'
} finally {
    if ($child) { Stop-LCServer $child }
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
Write-Host "$script:checks contract checks passed. Hardware inference was not run."
