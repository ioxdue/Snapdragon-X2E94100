<#
.SYNOPSIS
    Launcher e Auto-Configuratore Intelligente per llama.cpp su Snapdragon X2 Elite Extreme (Qualcomm Oryon / Hexagon NPU / Adreno GPU).

.DESCRIPTION
    Ispeziona direttamente i metadati binari del modello GGUF (architettura, tipo di quantizzazione,
    dimensione, conteggio layer, teste KV, MoE con esperti attivi, Sliding Window Attention)
    e calcola dinamicamente la combinazione ottimale di flag hardware per llama-server.exe
    basandosi sulla telemetria PMIC reale dello Snapdragon X2 Elite.

.PARAMETER Model
    Nome del file .gguf o percorso completo. Se omesso, avvia il top-performer.
    Ignorato in modalita' "Router" (tutti i modelli vengono configurati automaticamente).

.PARAMETER Port
    Porta HTTP del server (default: 18803).

.PARAMETER BindHost
    Indirizzo bind (default: 127.0.0.1; per accesso LAN occorre specificare 0.0.0.0 esplicitamente).

.PARAMETER DryRun
    Mostra i parametri calcolati e il comando CLI senza avviare il processo.

.PARAMETER CheckRuntime
    Verifica soltanto, senza aggiornare. Per default l'avvio installa/allinea il runtime a GenieX.

.PARAMETER Mode
    "Direct" (default): avvia direttamente llama-server sul modello specificato.
    "Router": avvia llama-server con --models-dir e un file INI generato automaticamente
              (--models-preset) contenente i flag ottimali per CIASCUN modello.

.PARAMETER ModelsMax
    In modalita' Router: massimo numero di modelli caricati in RAM contemporaneamente (default: 2).
    Un modello Q4_0 14B + KV cache a 64k occupa ~15 GB: con 2 modelli simultanei si saturano ~30 GB
    dei 48 GB totali, lasciando ~18 GB per Windows 11 e Cline.
#>

param(
    [string]$Model = "",
    [int]$Port = 18803,
    [string]$BindHost = "127.0.0.1",
    [switch]$DryRun,
    [ValidateSet("Direct", "Router")]
    [string]$Mode = "Direct",
    [switch]$ListOnly,
    [switch]$CheckRuntime,
    [int]$ModelsMax = 2,
    [string]$BinPath = (Join-Path $env:USERPROFILE "llama-cpp\llama-bin-win-cpu-arm64"),
    [string]$ModelsDir = (Join-Path $env:USERPROFILE ".cache\geniex\models"),
    [string]$GenieXDir = (Join-Path $env:LOCALAPPDATA "GenieX CLI")
)

$ErrorActionPreference = "Stop"

# Percorsi base
$ServerExe = Join-Path $BinPath "llama-server.exe"

# Prima preparazione inclusa: non richiede un llama-server gia' installato.
# Inventario e anteprima non installano il runtime e non richiedono l'eseguibile.
if (-not $ListOnly -and -not $DryRun) {
    $syncScript = Join-Path $PSScriptRoot "sync-geniex.ps1"
    & $syncScript -BinPath $BinPath -GenieXDir $GenieXDir -Apply:(-not $CheckRuntime)
    if ($LASTEXITCODE -ne 0) { throw "Preparazione runtime fallita (exit $LASTEXITCODE)." }
    if (-not (Test-Path -LiteralPath $ServerExe -PathType Leaf)) { throw "llama-server.exe non trovato dopo il bootstrap." }
}

# -------------------------------------------------------------
# Parser C# ad altissime prestazioni per metadati GGUF (< 100 ms)
# -------------------------------------------------------------
$csharpCode = @'
using System;
using System.IO;
using System.Text;

public class GgufModelMeta {
    public string Architecture = "";
    public string ModelName = "";
    public ulong ContextLength = 0;
    public ulong EmbeddingLength = 0;
    public ulong BlockCount = 0;
    public ulong HeadCount = 0;
    public ulong HeadCountKv = 0;
    public ulong ExpertCount = 0;
    public ulong ExpertUsedCount = 0;
    public ulong SlidingWindow = 0;
    public uint FileType = 0;
    public ulong FileSizeBytes = 0;
    public string QuantName = "UNKNOWN";

    public static GgufModelMeta Parse(string path) {
        GgufModelMeta m = new GgufModelMeta();
        FileInfo fi = new FileInfo(path);
        m.FileSizeBytes = (ulong)fi.Length;

        using (FileStream fs = File.OpenRead(path))
        using (BinaryReader br = new BinaryReader(fs)) {
            string magic = Encoding.ASCII.GetString(br.ReadBytes(4));
            if (magic != "GGUF") return null;
            uint version = br.ReadUInt32();
            ulong tensorCount = br.ReadUInt64();
            ulong kvCount = br.ReadUInt64();

            for (ulong i = 0; i < kvCount; i++) {
                ulong kLen = br.ReadUInt64();
                if (kLen > 500 || kLen > (ulong)(fs.Length - fs.Position))
                    throw new InvalidDataException("GGUF metadata key length invalid");
                string key = Encoding.UTF8.GetString(br.ReadBytes((int)kLen));
                uint valType = br.ReadUInt32();

                if (valType == 8) { // string
                    ulong sLen = br.ReadUInt64();
                    if (sLen > 16UL * 1024 * 1024 || sLen > (ulong)(fs.Length - fs.Position))
                        throw new InvalidDataException("GGUF metadata string length invalid");
                    string s = Encoding.UTF8.GetString(br.ReadBytes((int)sLen));
                    if (key == "general.architecture") m.Architecture = s;
                    if (key == "general.name") m.ModelName = s;
                } else if (valType == 4) { // uint32
                    uint v = br.ReadUInt32();
                    if (key == "general.file_type") m.FileType = v;
                    if (key.EndsWith(".context_length")) m.ContextLength = v;
                    if (key.EndsWith(".embedding_length")) m.EmbeddingLength = v;
                    if (key.EndsWith(".block_count")) m.BlockCount = v;
                    if (key.EndsWith(".attention.head_count")) m.HeadCount = v;
                    if (key.EndsWith(".attention.head_count_kv")) m.HeadCountKv = v;
                    if (key.EndsWith(".expert_count")) m.ExpertCount = v;
                    if (key.EndsWith(".expert_used_count")) m.ExpertUsedCount = v;
                    if (key.EndsWith(".attention.sliding_window")) m.SlidingWindow = v;
                } else if (valType == 10) { // uint64
                    ulong v = br.ReadUInt64();
                    if (key.EndsWith(".context_length")) m.ContextLength = v;
                    if (key.EndsWith(".embedding_length")) m.EmbeddingLength = v;
                    if (key.EndsWith(".block_count")) m.BlockCount = v;
                    if (key.EndsWith(".attention.head_count")) m.HeadCount = v;
                    if (key.EndsWith(".attention.head_count_kv")) m.HeadCountKv = v;
                    if (key.EndsWith(".expert_count")) m.ExpertCount = v;
                    if (key.EndsWith(".expert_used_count")) m.ExpertUsedCount = v;
                    if (key.EndsWith(".attention.sliding_window")) m.SlidingWindow = v;
                } else if (valType == 9) { // array
                    uint elemType = br.ReadUInt32();
                    ulong arrLen = br.ReadUInt64();
                    if (elemType == 0 || elemType == 1 || elemType == 7) fs.Seek((long)arrLen, SeekOrigin.Current);
                    else if (elemType == 2 || elemType == 3) fs.Seek((long)(arrLen * 2), SeekOrigin.Current);
                    else if (elemType == 4 || elemType == 5 || elemType == 6) fs.Seek((long)(arrLen * 4), SeekOrigin.Current);
                    else if (elemType == 10 || elemType == 11 || elemType == 12) fs.Seek((long)(arrLen * 8), SeekOrigin.Current);
                    else if (elemType == 8) {
                        for (ulong a = 0; a < arrLen; a++) {
                            ulong sl = br.ReadUInt64();
                            fs.Seek((long)sl, SeekOrigin.Current);
                        }
                    }
                } else if (valType == 0 || valType == 1 || valType == 7) { br.ReadByte(); }
                else if (valType == 2 || valType == 3) { br.ReadInt16(); }
                else if (valType == 5 || valType == 6) { br.ReadInt32(); }
                else if (valType == 11 || valType == 12) { br.ReadInt64(); }
                else { throw new InvalidDataException("Unsupported GGUF metadata value type " + valType); }
            }
        }
        if (m.HeadCountKv == 0 && m.HeadCount > 0) m.HeadCountKv = m.HeadCount;

        switch (m.FileType) {
            case 0: m.QuantName = "F32"; break;
            case 1: m.QuantName = "F16"; break;
            case 2: m.QuantName = "Q4_0"; break;
            case 3: m.QuantName = "Q4_1"; break;
            case 7: m.QuantName = "Q8_0"; break;
            case 8: m.QuantName = "Q5_0"; break;
            case 9: m.QuantName = "Q5_1"; break;
            // general.file_type uses llama_ftype, NOT ggml_type.
            case 10: case 11: case 12: case 13: case 14: case 15: case 16: case 17: case 18: case 21:
                m.QuantName = "K-Quants"; break;
            case 19: case 20: case 22: case 23: case 24: case 25: case 26: case 27: case 28: case 29: case 30: case 31:
                m.QuantName = "IQ-Quants"; break;
            case 32: m.QuantName = "BF16"; break;
            default: m.QuantName = "Type_" + m.FileType; break;
        }
        return m;
    }
}
'@

# In una sessione riusata il tipo puo' gia' essere caricato: si riusa senza mascherare
# errori di compilazione della prima definizione.
try {
    Add-Type -TypeDefinition $csharpCode -ErrorAction Stop
} catch {
    if (-not ('GgufModelMeta' -as [type])) { throw }
    Write-Verbose "GgufModelMeta gia' caricato in questa sessione PowerShell; si riusa la versione esistente."
}
if (-not ('GgufModelMeta' -as [type])) { throw "Parser GGUF non disponibile." }

# -------------------------------------------------------------
# Motore Decisionale Euristico basato sulla Telemetria Hardware
# -------------------------------------------------------------
function Get-HexagonOptimalConfig([GgufModelMeta]$meta) {
    $sizeGB = [Math]::Round($meta.FileSizeBytes / 1GB, 2)
    $isMoE = ($meta.ExpertCount -gt 0 -and $meta.ExpertUsedCount -gt 0)
    $hasSWA = ($meta.SlidingWindow -gt 0)
    $isKQuant = ($meta.QuantName -eq "K-Quants")

    $dev = "HTP0"
    $ts = $null
    $strategy = ""
    $ctx = 65536
    $ctxReason = ""

    # Regola 1: Selezione Dispositivo e Split Vettoriale
    if ($isMoE) {
        if ($sizeGB -le 13.5) {
            # es. LFM2.5 24B (12.65 GB, 4 attivi su 64):
            # Pure HTP0 fa 44.5 t/s, Hybrid 80/20 fa 46.0 t/s aprendo il canale crossbar della GPU
            $dev = "HTP0,GPUOpenCL"
            $ts = "80,20"
            $strategy = "MoE Leggero (LFM/A2B): Ibrido HTP0 (80%) + GPU (20%) per doppio canale di banda RAM (~46 t/s)"
        } else {
            # es. Flint Preview (23.3 GB) o Qwen 35B MoE (19.4 GB)
            if ($isKQuant) {
                # Kernel OpenCL Adreno lenti su K-Quants: concentrare 85% su HTP0
                $dev = "HTP0,GPUOpenCL"
                $ts = "85,15"
                $strategy = "MoE Gigante K-Quants (Flint): Ibrido HTP0 (85%) + GPU (15%) per preservare SIMD NPU"
            } else {
                # Q4_0 MoE (Qwen 35B MoE): split 50/50 evita picchi CPU a 41W e dimezza il consumo (88W -> 64W)
                $dev = "HTP0,GPUOpenCL"
                $ts = "50,50"
                $strategy = "MoE Gigante Q4_0: Ibrido 50/50 per azzerare il fallback CPU e quintuplicare il prefill (152 t/s)"
            }
        }
    } else {
        # Modelli Densi (Full Self-Attention)
        if ($sizeGB -le 6.0) {
            # es. Qwen 0.8B (0.5 GB), Spark 4B (2.4 GB), Ornith 9B (5.1 GB)
            $dev = "HTP0"
            $ts = $null
            $strategy = "Dense Compatto (<=6GB): 100% NPU HTP0 (prefill 750-3800 t/s, consumo 14-18W)"
        } elseif ($sizeGB -le 13.5) {
            # es. Qwen 14B (7.9 GB): Pure HTP0 crolla a 2.9 t/s su contesti lunghi per saturazione bus DMA!
            # Hybrid 75/25 scarica l'attention sulla GPU e raddoppia i canali fisici LPDDR5X
            $dev = "HTP0,GPUOpenCL"
            $ts = "75,25"
            $strategy = "Dense Medio (Qwen 14B): Ibrido HTP0 (75%) + GPU (25%) per sbloccare la saturazione DMA a contesti lunghi"
        } else {
            # es. Qwen 27B (15.2 GB): Oltre 15 GB FastRPC NPU collassa in fallback CPU
            $dev = "HTP0,GPUOpenCL"
            $ts = "50,50"
            $strategy = "Dense Enorme (>15GB): Ibrido 50/50 per prevenire overflow FastRPC e throttling termico"
        }
    }

    # Regola 2: Limite Buffer FastRPC (1 GiB) e Dimensione Contesto (-c)
    # Il driver Qualcomm qcnspmcdm8480.sys rifiuta singoli descrittori >= 1 GiB (1.073.741.824 byte) con errore 0x200.
    if ($hasSWA) {
        # Sliding Window Attention (es. Flint a 513 token): la cache attiva e' minuscola, 131k e' sicuro
        $ctx = [Math]::Min(131072, $meta.ContextLength)
        if ($ctx -eq 0) { $ctx = 131072 }
        $ctxReason = "Sliding Window attivo (window: $($meta.SlidingWindow)): 131k token sicuro e leggerissimo in RAM"
    } elseif ($isMoE) {
        $ctx = 65536
        $ctxReason = "MoE: 64k token ottimale (KV Cache nativa f16 senza collo di bottiglia)"
    } elseif ($sizeGB -ge 7.0) {
        # Dense 14B/27B: 128k sfora il limite di 1 GiB sul buffer FastRPC (1026 MB -> crash). 64k consuma 513 MB -> perfetto
        $ctx = 65536
        $ctxReason = "Dense >=7GB: limitato a 64k per rispettare il limite FastRPC NPU < 1 GiB (Error 0x200)"
    } else {
        $ctx = [Math]::Min(65536, $meta.ContextLength)
        if ($ctx -eq 0) { $ctx = 65536 }
        $ctxReason = "Contesto standard per coding agent (64k)"
    }

    # Non superare il limite di contesto GGUF, anche con MoE/dense.
    if ($meta.ContextLength -gt 0) {
        $ctx = [Math]::Min([long]$ctx, [long]$meta.ContextLength)
    }

    return [PSCustomObject]@{
        Device = $dev
        TensorSplit = $ts
        ContextSize = $ctx
        ContextReason = $ctxReason
        Strategy = $strategy
        IsMoE = $isMoE
        ActiveParamsEst = if ($isMoE) { "$($meta.ExpertUsedCount) / $($meta.ExpertCount) exp" } else { "100% dense" }
        SizeGB = $sizeGB
        Threads = 12
        ThreadsBatch = 12
        BatchSize = 4096
        UBatchSize = 512
    }
}

# -------------------------------------------------------------
# Scansione Modelli Disponibili e Auto-Linking
# -------------------------------------------------------------
# Esponi i .gguf dei sotto-repo (LiquidAI, Qwen, ecc.) nella root con hard-link a 0 byte
if (-not (Test-Path -LiteralPath $ModelsDir -PathType Container)) {
    throw "Cache modelli assente: $ModelsDir. Scaricare un GGUF con GenieX (geniex pull) oppure indicare -ModelsDir."
}
Get-ChildItem -Path $ModelsDir -Filter "*.gguf" -Recurse -File | ForEach-Object {
    if ($_.DirectoryName -ne $ModelsDir -and $_.Name -notlike "mmproj*") {
        $linkTarget = Join-Path $ModelsDir $_.Name
        if (-not (Test-Path $linkTarget)) {
            Write-Host "Creazione hard-link per modello in sotto-cartella: $($_.Name)" -ForegroundColor DarkGray
            cmd /c "mklink /H `"$linkTarget`" `"$($_.FullName)`"" 2>$null | Out-Null
            if (-not (Test-Path -LiteralPath $linkTarget -PathType Leaf)) {
                Write-Warning "Hard-link non creato per $($_.Name): il modello non sara' visibile al router (volume diverso?)."
            }
        }
    }
}

$allGguf = Get-ChildItem -Path $ModelsDir -Filter "*.gguf" -File | Where-Object { $_.Name -notlike "mmproj*" }
if ($allGguf.Count -eq 0) {
    Write-Error "Nessun file .gguf trovato in $ModelsDir"
    exit 1
}

if ($ListOnly) {
    Write-Host "`n=== MODELLI DISPONIBILI IN $ModelsDir ===" -ForegroundColor Cyan
    $table = foreach ($file in $allGguf) {
        $meta = [GgufModelMeta]::Parse($file.FullName)
        if ($meta) {
            $cfg = Get-HexagonOptimalConfig $meta
            [PSCustomObject]@{
                NomeFile = $file.Name
                Arch = $meta.Architecture
                Quant = $meta.QuantName
                SizeGB = $cfg.SizeGB
                Backend = $cfg.Device
                Split = if ($cfg.TensorSplit) { $cfg.TensorSplit } else { "100%" }
                Contesto = "$([Math]::Round($cfg.ContextSize / 1024))k"
            }
        }
    }
    $table | Format-Table -AutoSize
    exit 0
}

# =============================================================
# MODALITA' ROUTER: genera il file INI e avvia multi-modello
# =============================================================
if ($Mode -eq "Router") {
    Write-Host "`n========================================================" -ForegroundColor Cyan
    Write-Host " MODALITA' ROUTER: Auto-Configurazione per-modello" -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Cyan

    # Genera il file INI con preset per-modello
    $iniPath = Join-Path $PSScriptRoot "hexagon-models.ini"
    $iniLines = @()
    $iniLines += "version = 1"
    $iniLines += ""
    $iniLines += "# ============================================"
    $iniLines += "# Generato automaticamente da launch-hexagon.ps1"
    $iniLines += "# Data: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $iniLines += "# Piattaforma: Snapdragon X2 Elite Extreme"
    $iniLines += "# ============================================"
    $iniLines += ""
    $iniLines += "# Default globali applicati a tutti i modelli"
    $iniLines += "[*]"
    $iniLines += "flash-attn = on"
    $iniLines += "n-gpu-layers = 999"
    $iniLines += "parallel = 1"
    $iniLines += "load-mode = mlock"
    $iniLines += "fit = off"
    $iniLines += "threads = 12"
    $iniLines += "threads-batch = 12"
    $iniLines += "batch-size = 4096"
    $iniLines += "ubatch-size = 512"
    $iniLines += "context-shift = true"
    $iniLines += ""

    $modelCount = 0
    foreach ($file in $allGguf) {
        $meta = [GgufModelMeta]::Parse($file.FullName)
        if (-not $meta) { continue }
        # Salta i file non-modello (clip/mmproj) o troppo piccoli
        if ($meta.Architecture -eq "clip" -or $meta.Architecture -eq "") { continue }

        $cfg = Get-HexagonOptimalConfig $meta
        $alias = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)

        $iniLines += "# --- $($meta.ModelName) ($($cfg.SizeGB) GB, $($meta.Architecture), $($meta.QuantName)) ---"
        $iniLines += "# Strategia: $($cfg.Strategy)"
        $iniLines += "[$alias]"
        $iniLines += "model = $($file.FullName)"
        $iniLines += "device = $($cfg.Device)"
        if ($cfg.TensorSplit) {
            $iniLines += "tensor-split = $($cfg.TensorSplit)"
        }
        $iniLines += "ctx-size = $($cfg.ContextSize)"

        # Aggiungi mmproj se presente per modelli multimodali
        $mmprojCandidates = @(
            "mmproj-$($alias)-f16.gguf",
            "mmproj-$($alias)-BF16.gguf",
            "mmproj-$alias.gguf"
        )
        foreach ($mp in $mmprojCandidates) {
            $mpPath = Join-Path $ModelsDir $mp
            if (Test-Path $mpPath) {
                $iniLines += "mmproj = $mpPath"
                break
            }
        }

        $iniLines += ""
        $modelCount++

        Write-Host " [+] $alias" -ForegroundColor Green -NoNewline
        Write-Host " -> $($cfg.Device)$(if ($cfg.TensorSplit) { " [-ts $($cfg.TensorSplit)]" }) | ctx $([Math]::Round($cfg.ContextSize / 1024))k | $($cfg.SizeGB) GB" -ForegroundColor Gray
    }

    # Scrivi il file INI senza BOM UTF-8 (richiesto dal parser PEG di llama-server)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($iniPath, $iniLines, $utf8NoBom)
    Write-Host "`n Generato: $iniPath ($modelCount modelli configurati)" -ForegroundColor Yellow
    Write-Host " Modelli simultanei in RAM: max $ModelsMax" -ForegroundColor Yellow
    Write-Host "========================================================`n" -ForegroundColor Cyan

    # Costruisci gli argomenti del router
    $cliArgs = @(
        "--host", $BindHost,
        "--port", $Port,
        "--jinja",
        "--agent",
        "-lv", "3",
        "--models-dir", $ModelsDir,
        "--models-preset", $iniPath,
        "--models-max", $ModelsMax
    )

    # Configurazione Variabili d'Ambiente FastRPC / Hexagon
    $env:ADSP_LIBRARY_PATH = $BinPath
    $env:GGML_HEXAGON_OPBATCH = "512"
    $env:GGML_HEXAGON_VTCM_SIZE = "8M"

    if ($DryRun) {
        Write-Host "[DRY RUN] Comando che verrebbe eseguito:" -ForegroundColor Yellow
        Write-Host "& `"$ServerExe`" $($cliArgs -join ' ')" -ForegroundColor White
        Write-Host "`nContenuto hexagon-models.ini:" -ForegroundColor Yellow
        Get-Content $iniPath | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        exit 0
    }

    Write-Host "Avvio llama-server.exe ROUTER su porta $Port con $modelCount modelli..." -ForegroundColor Green
    Write-Host "Endpoint OpenAI compatibile: http://127.0.0.1:$Port/v1" -ForegroundColor Cyan
    Write-Host "I modelli vengono caricati on-demand alla prima richiesta.`n" -ForegroundColor Cyan

    Set-Location -Path $BinPath
    & $ServerExe @cliArgs
    exit $LASTEXITCODE
}

# =============================================================
# MODALITA' DIRECT: singolo modello con configurazione ottimale
# =============================================================

# Selezione del Modello
$targetModelFile = $null

if ($Model -ne "") {
    # Risolvi se percorso assoluto o nome file
    if (Test-Path $Model) {
        $targetModelFile = Get-Item $Model
    } else {
        $candidate = Join-Path $ModelsDir $Model
        if (Test-Path $candidate) {
            $targetModelFile = Get-Item $candidate
        } else {
            # Ricerca parziale
            $matches = $allGguf | Where-Object { $_.Name -like "*$Model*" }
            if (@($matches).Count -gt 1) {
                throw "Modello ambiguo: $Model. Specifica un nome GGUF esatto."
            }
            if (@($matches).Count -eq 1) {
                $targetModelFile = @($matches)[0]
            } else {
                Write-Error "Modello '$Model' non trovato in $ModelsDir"
                exit 1
            }
        }
    }
} else {
    # Default: Consiglia LFM2.5 24B se presente (il piu' veloce a ~46 t/s), altrimenti menu interattivo
    $preferred = $allGguf | Where-Object { $_.Name -like "*LFM2.5-24B*" } | Select-Object -First 1
    if ($preferred) {
        $targetModelFile = $preferred
        Write-Host "Nessun modello specificato. Selezione automatica del modello top-performer: $($preferred.Name)" -ForegroundColor Green
    } else {
        $targetModelFile = $allGguf[0]
    }
}

# -------------------------------------------------------------
# Analisi e Calcolo Parametri per il Modello Selezionato
# -------------------------------------------------------------
Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host " ANALISI HARDWARE METADATI: $($targetModelFile.Name)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$meta = [GgufModelMeta]::Parse($targetModelFile.FullName)
if (-not $meta) {
    Write-Error "Impossibile analizzare i metadati GGUF del file selezionato."
    exit 1
}

$cfg = Get-HexagonOptimalConfig $meta

Write-Host " * Nome Modello:      $($meta.ModelName)" -ForegroundColor White
Write-Host " * Architettura:      $($meta.Architecture) (Tipo: $($cfg.ActiveParamsEst))" -ForegroundColor White
Write-Host " * Formato Quant:     $($meta.QuantName) (ID: $($meta.FileType))" -ForegroundColor White
Write-Host " * Dimensione Disco:  $($cfg.SizeGB) GiB" -ForegroundColor White
Write-Host " * Layer / Teste KV:  $($meta.BlockCount) layer | $($meta.HeadCount) Q-heads | $($meta.HeadCountKv) KV-heads" -ForegroundColor White
if ($meta.SlidingWindow -gt 0) {
    Write-Host " * Sliding Window:    Attiva ($($meta.SlidingWindow) token)" -ForegroundColor Magenta
}
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
Write-Host " STRATEGIA DI ACCELERAZIONE OTTIMALE:" -ForegroundColor Yellow
Write-Host " * Backend:           $($cfg.Device) $(if ($cfg.TensorSplit) { "[-ts $($cfg.TensorSplit)]" })" -ForegroundColor Green
Write-Host " * Motivazione:       $($cfg.Strategy)" -ForegroundColor Gray
Write-Host " * Dimensione Ctx:    $($cfg.ContextSize) token ($([Math]::Round($cfg.ContextSize / 1024))k)" -ForegroundColor Green
Write-Host " * Vincolo Ctx:       $($cfg.ContextReason)" -ForegroundColor Gray
Write-Host " * CPU Oryon Core:    $($cfg.Threads) thread LLM (6 core riservati a Windows 11 / Cline)" -ForegroundColor Green
Write-Host " * Batching / FA:     -b $($cfg.BatchSize) -ub $($cfg.UBatchSize) -fa on --context-shift" -ForegroundColor Green
Write-Host "========================================================`n" -ForegroundColor Cyan

# -------------------------------------------------------------
# Costruzione Argomenti per llama-server.exe
# -------------------------------------------------------------
$cliArgs = @(
    "--host", $BindHost,
    "--port", $Port,
    "--jinja",
    "--agent",
    "-np", "1",
    "-lm", "mlock",
    "-lv", "3",
    "-ngl", "999",
    "-c", $cfg.ContextSize,
    "--context-shift",
    "-b", $cfg.BatchSize,
    "-ub", $cfg.UBatchSize,
    "-tb", $cfg.ThreadsBatch,
    "-t", $cfg.Threads,
    "-fit", "off",
    "-dev", $cfg.Device,
    "-fa", "on"
)

if ($cfg.TensorSplit) {
    $cliArgs += @("-ts", $cfg.TensorSplit)
}

$cliArgs += @("-m", $targetModelFile.FullName, "--alias", [System.IO.Path]::GetFileNameWithoutExtension($targetModelFile.Name))

# -------------------------------------------------------------
# Configurazione Variabili d'Ambiente FastRPC / Hexagon
# -------------------------------------------------------------
$env:ADSP_LIBRARY_PATH = $BinPath
$env:GGML_HEXAGON_OPBATCH = "512"
$env:GGML_HEXAGON_VTCM_SIZE = "8M"

if ($DryRun) {
    Write-Host "[DRY RUN] Comando che verrebbe eseguito:" -ForegroundColor Yellow
    Write-Host "& `"$ServerExe`" $($cliArgs -join ' ')" -ForegroundColor White
    exit 0
}

Write-Host "Avvio llama-server.exe su porta $Port..." -ForegroundColor Green
Write-Host "Endpoint OpenAI compatibile per Cline: http://127.0.0.1:$Port/v1`n" -ForegroundColor Cyan

Set-Location -Path $BinPath
& $ServerExe @cliArgs
exit $LASTEXITCODE
