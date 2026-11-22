import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

import kilo_config

# Campiona i contatori GPU Engine e Processo e li riduce a massimi per gruppo.
# La NPU via FastRPC non espone contatori Windows: viene riportata n/d
# per onesta', non per mancanza di campionamento. L'OpenCL dell'Adreno
# compare nel motore "3D" di GPU Engine.
_ACCELERATOR_PS = r"""
$ErrorActionPreference = 'Stop'
$procSet = Get-Counter -ListSet 'Process' -ErrorAction SilentlyContinue
$cpuPath = $null
if ($procSet) {
    foreach ($p in $procSet.Paths) {
        if ($p -match '%\s*(Processor Time|Tempo processore|Tiempo procesador|Prozessorzeit|Temps processeur)\s*$') {
            $cpuPath = $p
            break
        }
    }
}
$paths = @('\GPU Engine(*)\Utilization Percentage')
if ($cpuPath) { $paths += $cpuPath }
$data = Get-Counter $paths -SampleInterval 1 -MaxSamples __SAMPLES__ -ErrorAction Stop
if ($data -isnot [array]) { $data = @($data) }
$gpuMax = @{}
$cpuByTs = @{}
foreach ($g in $data) {
    $ts = $g.Timestamp.Ticks
    foreach ($c in $g.CounterSamples) {
        $p = $c.Path
        if ($p -match 'gpu engine.*luid_([0-9a-fx_]+)_phys_0_eng_[0-9]+_engtype_([a-z0-9 ]+?)\)') {
            $k = $matches[1] + '|' + $matches[2]
            $v = [double]$c.CookedValue
            if (-not $gpuMax.ContainsKey($k) -or $v -gt $gpuMax[$k]) { $gpuMax[$k] = $v }
        } elseif ($cpuPath -and $p -match 'processo?\(([^)]+)\)' -and $matches[1] -like 'llama-server*') {
            $cpuByTs[$ts] = $cpuByTs[$ts] + [double]$c.CookedValue
        }
    }
}
$cpuPeak = 0.0
foreach ($t in $cpuByTs.Keys) { $v = $cpuByTs[$t] / 100.0; if ($v -gt $cpuPeak) { $cpuPeak = $v } }
foreach ($k in $gpuMax.Keys) { Write-Output ("GPUMAX|" + $k + "|" + [math]::Round($gpuMax[$k], 1)) }
Write-Output ("CPUSUM|" + [math]::Round($cpuPeak, 2))
"""


class DiagnosticsController:
    def __init__(self, workspace_dir: Path, stack_manager, settings, processes):
        self.workspace_dir = Path(workspace_dir)
        self.stack_manager = stack_manager
        self.settings = settings
        self.processes = processes

    def prerequisites(self) -> List[Dict[str, str]]:
        checks = []

        def add(name, status, detail):
            checks.append({"name": name, "status": status, "detail": str(detail)})

        architecture = platform.machine()
        add("Windows 11 ARM64", "OK" if os.name == "nt" and architecture.upper() == "ARM64" else "ERRORE",
            f"{platform.system()} {architecture}")
        add("Python TUI", "OK", platform.python_version())
        node = shutil.which("node")
        kilo = shutil.which("kilo.cmd") or shutil.which("kilo")
        add("Node.js", "OK" if node else "ERRORE", node or "Non trovato")
        add("Kilo Code CLI", "OK" if kilo else "ERRORE", kilo or "Non trovato")
        status = self.stack_manager.get_stack_status()
        add("GenieX CLI", "OK" if status["geniex_installed"] else "ERRORE", status["geniex_version"])
        add("llama-server", "OK" if status["llama_server_installed"] else "ERRORE", status["llama_server_version"])
        add("Allineamento commit", "OK" if status["commit_aligned"] else "ATTENZIONE",
            "Corrispondente" if status["commit_aligned"] else "Differenti o non rilevati")
        add("Integrità runtime", "OK" if status["all_files_aligned"] else "ERRORE",
            f"{len(status['mismatched_files'])} differenze, {len(status['missing_files'])} mancanti")
        artifact = status["npu_artifacts"].get("libggml-htp.cat", {})
        signature = artifact.get("signature", "N/D")
        add("Catalogo Authenticode HTP", "OK" if signature == "Valida" else "ATTENZIONE", signature)
        add("FastRPC / offload reale", "N/D", "Richiede inferenza e log; la presenza dei file non lo dimostra")
        for directory in self.settings.models_dirs:
            count = sum(1 for _ in directory.rglob("*.gguf")) if directory.is_dir() else 0
            add(f"Cache GGUF: {directory}", "OK" if count else "ATTENZIONE", f"{count} file")
        config_path = self.workspace_dir / "kilo.json"
        valid, issues = kilo_config.validate_kilo_config(config_path)
        add("Kilo loopback e permessi", "OK" if valid else "ERRORE", "; ".join(issues) or "Provider locale e permessi ask")
        if config_path.is_file():
            try:
                import json
                config = json.loads(config_path.read_text(encoding="utf-8"))
                keys = [p.get("options", {}).get("apiKey") for p in config.get("provider", {}).values()]
                safe = all(key in (None, "", "local") for key in keys)
                add("Credenziali cloud", "OK" if safe else "ERRORE", "Nessun token configurato" if safe else "Token non locale rilevato")
            except (OSError, ValueError, TypeError):
                add("Credenziali cloud", "ERRORE", "Configurazione Kilo non leggibile")
        parsed = urlsplit(self.processes.base_url)
        add("Endpoint TUI", "OK" if parsed.hostname in ("127.0.0.1", "localhost", "::1") else "ERRORE",
            self.processes.base_url)
        return checks

    def port_open(self, port: int) -> bool:
        if not 1 <= int(port) <= 65535:
            raise ValueError("Porta non valida")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
                return True
        except OSError:
            return False

    def accelerator_usage(self, samples: int = 3) -> str:
        """Utilizzo osservato di GPU e CPU del server in una finestra di campionamento.

        Ritorna una riga compatta: GPU % (motore 3D dell'Adreno, include OpenCL),
        CPU di llama-server in core-equivalenti (indicatore di fallback CPU) e
        NPU dichiarata n/d perche' il trasporto FastRPC non espone contatori.
        """
        if os.name != "nt":
            return "N/D; solo Windows"
        script = _ACCELERATOR_PS.replace("__SAMPLES__", str(max(1, int(samples))))
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15 + 2 * samples,
            )
        except (OSError, subprocess.SubprocessError):
            return "N/D; contatori performance non disponibili"
        if proc.returncode != 0:
            return "N/D; contatori performance non disponibili"
        gpu = 0.0
        cpu = 0.0
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split("|")]
            try:
                if len(parts) == 4 and parts[0] == "GPUMAX" and parts[2] == "3d":
                    gpu = max(gpu, float(parts[3]))
                elif len(parts) == 2 and parts[0] == "CPUSUM":
                    cpu = float(parts[1])
            except ValueError:
                continue
        label = f"GPU {gpu:.0f}% · llama-server CPU {cpu:.1f} core · NPU n/d (FastRPC)"
        if gpu < 15.0 and cpu < 0.1:
            label += "; fermo — usa B benchmark sotto carico"
        return label

    def recent_logs(self, errors_only: bool = False, limit: int = 8) -> str:
        paths = list(self.workspace_dir.glob("*.log")) + list(self.workspace_dir.glob("*.txt"))
        paths += list(self.processes.log_dir.glob("*.log"))
        existing = []
        for path in paths:
            try:
                existing.append((path.stat().st_mtime, path))
            except OSError:
                pass
        blocks = []
        for _, path in sorted(existing, reverse=True)[:limit]:
            text = self.processes._read_log(path)
            lines = text.splitlines()
            if errors_only:
                lines = [line for line in lines if any(word in line.lower()
                         for word in ("error", "failed", "exception", "errore", "warning"))]
            if lines:
                blocks.append(f"--- {path} ---\n" + "\n".join(lines[-100:]))
        return "\n\n".join(blocks) or "Nessuna riga di log corrispondente"
