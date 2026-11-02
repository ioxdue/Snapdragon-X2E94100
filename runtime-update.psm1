# Windows PowerShell 5.1 / .NET Framework: nessun Python, Git, SDK o archiviatore esterno.
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

function Test-CommitEqual([string]$Left, [string]$Right) {
    return ($Left -match '^[a-fA-F0-9]{7,40}$' -and $Right -match '^[a-fA-F0-9]{7,40}$' -and
        ($Left.StartsWith($Right, [StringComparison]::OrdinalIgnoreCase) -or
         $Right.StartsWith($Left, [StringComparison]::OrdinalIgnoreCase)))
}

function Invoke-RuntimeNative([string]$File, [string]$Arguments, [string]$Directory, [int]$Timeout = 30000) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $File
    $start.Arguments = $Arguments
    $start.WorkingDirectory = $Directory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables['ADSP_LIBRARY_PATH'] = $Directory
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Timeout di $File $Arguments"
        }
        $process.WaitForExit()
        $text = $stdout.Result + "`n" + $stderr.Result
        if ($process.ExitCode -ne 0) { throw "$File $Arguments : exit $($process.ExitCode): $text" }
        return $text
    } finally { $process.Dispose() }
}

function Get-GenieRuntime([string]$GenieXDir) {
    $defaultGenie = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'GenieX CLI')).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $normalizedGenie = if ($GenieXDir) { [IO.Path]::GetFullPath($GenieXDir).TrimEnd([IO.Path]::DirectorySeparatorChar) } else { '' }
    $exe = if ($GenieXDir) { Join-Path $GenieXDir 'geniex.exe' } else { '' }
    $exeExists = if ($exe) { Test-Path -LiteralPath $exe -PathType Leaf } else { $false }
    if (-not $exeExists -and (-not $GenieXDir -or $normalizedGenie -eq $defaultGenie)) {
        $command = Get-Command geniex.exe -ErrorAction SilentlyContinue
        if ($command) {
            $exe = $command.Source
            $GenieXDir = Split-Path -Parent $exe
            $exeExists = Test-Path -LiteralPath $exe -PathType Leaf
        }
    }
    if (-not $exeExists) { throw "Installare GenieX CLI Qualcomm: geniex.exe non trovato in $GenieXDir o nel PATH." }
    $version = Invoke-RuntimeNative $exe '--version --skip-update' $GenieXDir
    $hash = [regex]::Match($version, 'LlamaCPP Runtime Hash:\s*([a-fA-F0-9]{7,40})\b').Groups[1].Value
    if (-not $hash) { throw 'GenieX non ha restituito un LlamaCPP Runtime Hash valido.' }
    $runtime = Join-Path $GenieXDir 'llama_cpp'
    $files = @(Get-ChildItem -LiteralPath $runtime -File -ErrorAction Stop)
    foreach ($name in @('ggml.dll', 'ggml-hexagon.dll', 'ggml-opencl.dll', 'libggml-htp.cat')) {
        if (-not ($files.Name -contains $name)) { throw "Runtime GenieX incompleto: manca $name in $runtime." }
    }
    if (-not ($files.Name -like 'libggml-htp-v*.so')) { throw "Artefatti HTP non trovati in $runtime." }
    return [pscustomobject]@{ Exe = $exe; Directory = $GenieXDir; Runtime = $runtime; Files = $files; Commit = $hash }
}

function Get-FrontendRuntime([string]$Directory) {
    $server = Join-Path $Directory 'llama-server.exe'
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { return $null }
    try {
        $version = Invoke-RuntimeNative $server '--version' $Directory
        $match = [regex]::Match($version, '(?i)\bcommit\s+([a-f0-9]{7,40})\b|\(([a-f0-9]{7,40})\)')
        $hash = $match.Groups[1].Value
        if (-not $hash) { $hash = $match.Groups[2].Value }
        $devices = Invoke-RuntimeNative $server '--list-devices' $Directory
        if (-not ($devices -match '\bHTP0\b' -and $devices -match '\bGPUOpenCL\b')) {
            try {
                $devCheck = Invoke-RuntimeNative $server '-dev HTP0,GPUOpenCL --version' $Directory
                if ($devCheck -match '(?i)\b(?:commit|version)\b') {
                    $devices = 'HTP0 GPUOpenCL'
                }
            } catch { }
        }
        return [pscustomobject]@{ Commit = $hash; Devices = $devices; Version = $version.Trim() }
    } catch { return $null }
}

function Test-GenieFiles($Genie, [string]$Directory) {
    foreach ($file in $Genie.Files) {
        $target = Join-Path $Directory $file.Name
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { return $false }
        if ((Get-Item -LiteralPath $target).Length -ne $file.Length -or
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash) { return $false }
    }
    return $true
}

function Test-FrontendRuntime($Frontend, [string]$Commit) {
    return ($null -ne $Frontend -and (Test-CommitEqual $Frontend.Commit $Commit) -and
        $Frontend.Devices -match '\bHTP0\b' -and $Frontend.Devices -match '\bGPUOpenCL\b')
}

function Get-RuntimeJson([string]$Url) {
    # Invoke-RestMethod puo' emettere un array JSON come un solo oggetto di pipeline.
    # Assegna prima il risultato: il return della variabile ne enumera gli elementi.
    $data = Invoke-RestMethod -Uri $Url -Headers @{ 'User-Agent' = 'Snapdragon-X2E94100'; Accept = 'application/vnd.github+json' } -TimeoutSec 30
    return $data
}

function Get-TagCommit([string]$Tag) {
    $ref = Get-RuntimeJson "https://api.github.com/repos/ggml-org/llama.cpp/git/ref/tags/$Tag"
    $object = $ref.object
    for ($i = 0; $object.type -eq 'tag' -and $i -lt 5; $i++) {
        $object = (Get-RuntimeJson "https://api.github.com/repos/ggml-org/llama.cpp/git/tags/$($object.sha)").object
    }
    if ($object.type -ne 'commit') { throw "Tag non risolto a un commit: $Tag" }
    return $object.sha
}

function Get-LlamaReleaseAsset([string]$Commit) {
    $base = 'https://api.github.com/repos/ggml-org/llama.cpp'
    $resolved = (Get-RuntimeJson "$base/commits/$Commit").sha
    if (-not (Test-CommitEqual $resolved $Commit)) { throw 'Commit GenieX non risolto da GitHub.' }
    $tag = $null
    # Stima usata solo per cercare: ogni candidato deve poi avere esattamente lo SHA richiesto.
    $latest = @(Get-RuntimeJson "$base/releases?per_page=5") | Where-Object { $_.tag_name -match '^b\d+$' } | Select-Object -First 1
    if ($latest) {
        $compare = Get-RuntimeJson "$base/compare/${resolved}...$($latest.tag_name)"
        $estimate = [int]$latest.tag_name.Substring(1) - [int]$compare.ahead_by + [int]$compare.behind_by
        foreach ($offset in @(0, -1, 1, -2, 2, -3, 3, -4, 4)) {
            $candidate = "b$($estimate + $offset)"
            try { $candidateCommit = Get-TagCommit $candidate } catch {
                # Un 404 e' atteso per b* senza tag; gli altri errori non autorizzano fallback approssimativi.
                if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response -and
                    [int]$_.Exception.Response.StatusCode -eq 404) { continue }
                throw
            }
            if ($candidateCommit -eq $resolved) { $tag = $candidate; break }
        }
    }
    if (-not $tag) {
        for ($page = 1; $page -le 10; $page++) {
            $tags = @(Get-RuntimeJson "$base/tags?per_page=100&page=$page")
            $match = $tags | Where-Object { $_.commit.sha -eq $resolved } | Select-Object -First 1
            if ($match) { $tag = $match.name; break }
            if ($tags.Count -lt 100) { break }
        }
    }
    if (-not $tag) { throw "Nessuna release identificata per il commit GenieX $Commit. Installazione esistente conservata." }
    $release = Get-RuntimeJson "$base/releases/tags/$tag"
    $asset = @($release.assets | Where-Object { $_.name -match 'bin-win-cpu-arm64\.zip$' })
    if ($asset.Count -ne 1) { throw "Asset Windows ARM64 CPU assente o ambiguo nella release $tag." }
    if (-not ($asset[0].PSObject.Properties.Name -contains 'digest') -or $asset[0].digest -notmatch '^sha256:[a-fA-F0-9]{64}$') {
        throw "SHA-256 dell'asset $($asset[0].name) non disponibile su GitHub."
    }
    return [pscustomobject]@{ Tag = $tag; Commit = $resolved; Url = $asset[0].browser_download_url;
        Size = [long]$asset[0].size; Sha256 = $asset[0].digest.Substring(7) }
}

function Get-RuntimeArchive($Asset, [string]$Destination) {
    $uri = [uri]$Asset.Url
    if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'github.com' -or
        -not $uri.AbsolutePath.StartsWith('/ggml-org/llama.cpp/releases/download/')) { throw 'URL asset non ufficiale.' }
    $request = [Net.HttpWebRequest]::Create($uri)
    $request.UserAgent = 'Snapdragon-X2E94100'
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $response = $null; $output = $null
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { throw "Download HTTP $($response.StatusCode)" }
        $output = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew)
        $response.GetResponseStream().CopyTo($output)
    } finally {
        if ($output) { $output.Dispose() }
        if ($response) { $response.Dispose() }
    }
    Assert-RuntimeArchive $Asset $Destination
}

function Assert-RuntimeArchive($Asset, [string]$Path) {
    if ((Get-Item -LiteralPath $Path).Length -ne $Asset.Size -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Asset.Sha256) {
        throw 'Archivio incompleto o SHA-256 differente: nessuna installazione eseguita.'
    }
}

function Expand-RuntimeArchive([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    $seen = @{}
    try {
        foreach ($entry in $zip.Entries) {
            if (-not $entry.Name) { continue }
            if ($entry.FullName -match '(^|[\\/])\.\.([\\/]|$)|^[\\/]|:') { throw 'Percorso ZIP non valido.' }
            $name = $entry.Name
            if ($seen.ContainsKey($name)) { throw "Nome duplicato nello ZIP: $name" }
            $seen[$name] = $true
            $target = Join-Path $Destination $name
            $stream = $null; $output = $null
            try {
                $stream = $entry.Open()
                $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
                $stream.CopyTo($output)
                if ($output.Length -ne $entry.Length) { throw "File ZIP troncato: $name" }
            } finally {
                if ($output) { $output.Dispose() }
                if ($stream) { $stream.Dispose() }
            }
        }
    } finally { $zip.Dispose() }
    foreach ($name in @('llama-server.exe', 'llama-cli.exe', 'llama-bench.exe')) {
        if (-not $seen.ContainsKey($name)) { throw "Frontend obbligatorio assente nello ZIP: $name" }
    }
}

function Assert-Arm64Executable([string]$Path) {
    $reader = New-Object IO.BinaryReader([IO.File]::OpenRead($Path))
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw "EXE non PE: $Path" }
        $reader.BaseStream.Position = 0x3C
        $offset = $reader.ReadUInt32()
        $reader.BaseStream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x00004550 -or $reader.ReadUInt16() -ne 0xAA64) { throw "EXE non ARM64: $Path" }
    } finally { $reader.Dispose() }
}

function Test-FrontendFiles([string]$Directory) {
    foreach ($name in @('llama-server.exe', 'llama-cli.exe', 'llama-bench.exe')) {
        $path = Join-Path $Directory $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        try { Assert-Arm64Executable $path } catch { return $false }
    }
    return $true
}

function Get-RuntimeProcesses([string]$Directory) {
    $server = Join-Path $Directory 'llama-server.exe'
    return @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath -eq $server })
}

function Stop-RuntimeProcesses($Processes) {
    foreach ($process in $Processes) {
        if (-not $process.CommandLine) { throw "Command line non disponibile per PID $($process.ProcessId): arresto annullato." }
    }
    foreach ($process in $Processes) {
        $running = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        if ($running) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            if (-not $running.WaitForExit(10000)) { throw "Processo PID $($process.ProcessId) ancora attivo." }
        }
    }
}

function Start-RuntimeProcesses($Processes, [string]$Directory) {
    $ids = @($Processes | ForEach-Object { $_.ProcessId })
    foreach ($process in $Processes) {
        if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) { continue }
        # Il router ricrea i propri figli; riavvia soltanto le radici della fotografia acquisita.
        if ($ids -contains $process.ParentProcessId) { continue }
        $arguments = [regex]::Replace($process.CommandLine, '^\s*(?:"[^"]+"|\S+)\s*', '')
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $process.ExecutablePath
        $start.Arguments = $arguments
        $start.WorkingDirectory = $Directory
        $start.UseShellExecute = $false
        $start.EnvironmentVariables['ADSP_LIBRARY_PATH'] = $Directory
        $child = [Diagnostics.Process]::Start($start)
        $child.Dispose()
    }
}

function Move-RuntimeDirectory([string]$Source, [string]$Destination) {
    [IO.Directory]::Move($Source, $Destination)
}

function Sync-HexagonRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$BinPath, [Parameter(Mandatory)][string]$GenieXDir,
        [switch]$Apply, [switch]$Force, [switch]$NoStopServer, [switch]$UseHardLinks)
    $BinPath = [IO.Path]::GetFullPath($BinPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $GenieXDir = [IO.Path]::GetFullPath($GenieXDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $parent = Split-Path -Parent $BinPath
    if (-not $parent -or $BinPath.StartsWith($GenieXDir + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $GenieXDir.StartsWith($BinPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $BinPath -eq $GenieXDir) {
        throw 'BinPath deve essere una directory separata dall''installazione GenieX.'
    }
    $genie = Get-GenieRuntime $GenieXDir
    $frontend = Get-FrontendRuntime $BinPath
    $aligned = (Test-FrontendRuntime $frontend $genie.Commit) -and (Test-FrontendFiles $BinPath) -and (Test-GenieFiles $genie $BinPath)
    if ($aligned -and -not $Force) {
        return [pscustomobject]@{ Updated = $false; Commit = $genie.Commit; Files = @($genie.Files.Name);
            Backup = ''; Message = 'Runtime gia'' allineato a GenieX; nessun download necessario.' }
    }
    if (-not $Apply) { throw 'Runtime assente o non allineato. Usare -Apply per installare/aggiornare automaticamente.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    [void][IO.Directory]::CreateDirectory($parent)
    $lockPath = $BinPath + '.update.lock'
    $lock = $null
    try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Un altro aggiornamento del runtime e'' in corso.' }
    $suffix = [guid]::NewGuid().ToString('N')
    $stage = $BinPath + '.stage-' + $suffix
    $backup = $BinPath + '.backup-' + $suffix
    $archive = $BinPath + '.download-' + $suffix + '.zip'
    $oldMoved = $false; $newPlaced = $false; $processes = @(); $stopped = $false
    $restartError = ''; $asset = $null
    try {
        [void][IO.Directory]::CreateDirectory($stage)
        $reuse = $frontend -and (Test-CommitEqual $frontend.Commit $genie.Commit) -and (Test-FrontendFiles $BinPath)
        # Mantieni configurazioni e file aggiuntivi. Le copie di staging non condividono inode con il backup.
        if (Test-Path -LiteralPath $BinPath) {
            Get-ChildItem -LiteralPath $BinPath -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force }
        }
        if (-not $reuse) {
            $asset = Get-LlamaReleaseAsset $genie.Commit
            Get-RuntimeArchive $asset $archive
            $extracted = Join-Path $stage '.download'
            [void][IO.Directory]::CreateDirectory($extracted)
            Expand-RuntimeArchive $archive $extracted
            Get-ChildItem -LiteralPath $extracted -File | ForEach-Object {
                $destination = Join-Path $stage $_.Name
                if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
                Move-Item -LiteralPath $_.FullName -Destination $destination
            }
            Remove-Item -LiteralPath $extracted -Recurse -Force
        }
        foreach ($file in $genie.Files) {
            $target = Join-Path $stage $file.Name
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
            $linked = $false
            if ($UseHardLinks) {
                try { New-Item -ItemType HardLink -Path $target -Target $file.FullName -ErrorAction Stop | Out-Null; $linked = $true }
                catch { $linked = $false }
            }
            if (-not $linked) { Copy-Item -LiteralPath $file.FullName -Destination $target }
        }
        foreach ($name in @('llama-server.exe', 'llama-cli.exe', 'llama-bench.exe')) { Assert-Arm64Executable (Join-Path $stage $name) }
        if (-not (Test-GenieFiles $genie $stage)) { throw 'Verifica SHA-256 dei file GenieX in staging fallita.' }
        $stagedFrontend = Get-FrontendRuntime $stage
        if (-not (Test-FrontendRuntime $stagedFrontend $genie.Commit)) { throw 'Frontend in staging non allineato o device HTP0/GPUOpenCL non enumerati.' }
        $manifestFiles = @(Get-ChildItem -LiteralPath $stage -File | Where-Object { $_.Name -ne 'hexagon-runtime.json' } | ForEach-Object {
            [pscustomobject]@{ Name = $_.Name; Size = $_.Length; Sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
        })
        $manifest = [pscustomobject]@{ GenieXCommit = $genie.Commit; FrontendVersion = $stagedFrontend.Version;
            Release = $asset; InstalledUtc = [DateTime]::UtcNow.ToString('o'); Files = $manifestFiles }
        [IO.File]::WriteAllText((Join-Path $stage 'hexagon-runtime.json'), ($manifest | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
        $processes = @(Get-RuntimeProcesses $BinPath)
        if ($processes.Count -gt 0) {
            if ($NoStopServer) { throw 'llama-server della destinazione attivo; aggiornamento annullato senza sostituzioni.' }
            $stopped = $true  # Anche un arresto parziale deve tentare il ripristino delle radici.
            Stop-RuntimeProcesses $processes
        }
        if (Test-Path -LiteralPath $BinPath) { Move-RuntimeDirectory $BinPath $backup; $oldMoved = $true }
        Move-RuntimeDirectory $stage $BinPath
        $newPlaced = $true
        if (-not (Test-FrontendRuntime (Get-FrontendRuntime $BinPath) $genie.Commit)) { throw 'Verifica dopo la sostituzione fallita.' }
    } catch {
        $failure = $_.Exception.Message
        try {
            if ($newPlaced -and (Test-Path -LiteralPath $BinPath)) { Remove-Item -LiteralPath $BinPath -Recurse -Force }
            if ($oldMoved) { Move-RuntimeDirectory $backup $BinPath; $oldMoved = $false }
        } catch { throw "Aggiornamento fallito: $failure. Ripristino non completato: $($_.Exception.Message). Backup conservato: $backup" }
        throw "Aggiornamento fallito: $failure. Installazione precedente conservata/ripristinata."
    } finally {
        if ($stopped) {
            try { Start-RuntimeProcesses $processes $BinPath }
            catch { $restartError = $_.Exception.Message; Write-Warning "Riavvio non completato: $restartError" }
        }
        foreach ($temporary in @($stage, $archive)) {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
        }
        $lock.Dispose()
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
    if ($restartError) { throw "Runtime aggiornato, ma riavvio del server non completato: $restartError. Backup: $backup" }
    return [pscustomobject]@{ Updated = $true; Commit = $genie.Commit; Files = @($genie.Files.Name);
        Backup = $(if ($oldMoved) { $backup } else { '' }); Message = 'Runtime installato/aggiornato e verificato; backup precedente conservato se presente.' }
}

Export-ModuleMember -Function Sync-HexagonRuntime, Get-GenieRuntime, Invoke-RuntimeNative
