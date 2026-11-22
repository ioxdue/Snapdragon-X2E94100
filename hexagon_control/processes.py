import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .results import OperationResult


@dataclass
class OwnedServer:
    launcher_pid: int
    server_pid: Optional[int]
    mode: str
    port: int
    model: str
    started_at: float
    stdout_log: Path
    stderr_log: Path


class ProcessController:
    def __init__(self, workspace_dir: Path, stack_manager, settings, base_url: str):
        self.workspace_dir = Path(workspace_dir)
        self.stack_manager = stack_manager
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        self.owned: Dict[int, OwnedServer] = {}
        self.children: Dict[int, subprocess.Popen] = {}
        self.log_dir = settings.path.parent / "logs"
        self.http = httpx.Client(timeout=httpx.Timeout(10.0, connect=1.5), trust_env=False)

    def close(self):
        self.http.close()

    def set_port(self, port: int):
        if not 1 <= int(port) <= 65535:
            raise ValueError("Porta non valida")
        self.base_url = f"http://127.0.0.1:{int(port)}/v1"
        self.settings.data["port"] = int(port)
        self.settings.save()

    def health(self) -> Tuple[bool, float, List[str]]:
        started = time.perf_counter()
        try:
            response = self.http.get(self.base_url + "/models", timeout=2.0)
            response.raise_for_status()
            models = [
                item["id"] for item in response.json().get("data", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
            ]
            return True, (time.perf_counter() - started) * 1000, models
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False, 0.0, []

    @staticmethod
    def validate_bind(bind_host: str, allow_non_loopback: bool = False):
        if bind_host in ("127.0.0.1", "localhost", "::1"):
            return
        if not allow_non_loopback:
            raise ValueError("Binding non-loopback richiede conferma esplicita")
        if not bind_host.strip():
            raise ValueError("Host di binding non valido")

    def server_processes(self, managed_only: bool = False) -> List[Dict]:
        if sys.platform != "win32":
            return []
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" -ErrorAction Stop | "
            "Select-Object ProcessId,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=True,
            )
            if not proc.stdout.strip():
                return []
            data = json.loads(proc.stdout)
            items = data if isinstance(data, list) else [data]
            processes = [{
                "pid": int(item["ProcessId"]), "path": item.get("ExecutablePath") or "",
                "command_line": item.get("CommandLine") or "",
                "started": str(item.get("CreationDate") or ""), "memory_mb": 0,
            } for item in items]
            if managed_only:
                managed = set(self.stack_manager.managed_server_pids())
                processes = [item for item in processes if item["pid"] in managed]
            if processes:
                memory = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-Process -Name llama-server -ErrorAction Stop | Select-Object Id,WorkingSet64 | ConvertTo-Json -Compress"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=10, check=True,
                )
                values = json.loads(memory.stdout) if memory.stdout.strip() else []
                values = values if isinstance(values, list) else [values]
                working_sets = {int(item["Id"]): round(float(item["WorkingSet64"]) / 1048576, 1) for item in values}
                for item in processes:
                    item["memory_mb"] = working_sets.get(item["pid"], 0)
            return processes
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
            return []

    def start(self, mode: str, model: Optional[Path] = None, port: Optional[int] = None,
              models_max: Optional[int] = None, bind_host: str = "127.0.0.1",
              allow_non_loopback: bool = False, wait: float = 30.0) -> OperationResult:
        try:
            self.validate_bind(bind_host, allow_non_loopback)
            port = int(port or self.settings.data["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Porta non valida")
            models_max = int(models_max or self.settings.data["models_max"])
            if models_max <= 0:
                raise ValueError("models-max deve essere positivo")
            if mode not in ("Router", "Direct"):
                raise ValueError("Modalità server non valida")
            if mode == "Direct" and (model is None or not Path(model).is_file()):
                raise ValueError("Selezionare un GGUF esistente")
        except ValueError as exc:
            return OperationResult(False, str(exc))
        host = f"[{bind_host}]" if ":" in bind_host else bind_host
        self.base_url = f"http://{host}:{port}/v1"
        self.settings.data["port"] = port
        online, _, _ = self.health()
        if online:
            return OperationResult(False, "L'endpoint è già occupato da un server attivo")
        launcher = self.workspace_dir / "launch-hexagon.ps1"
        if sys.platform != "win32" or not launcher.is_file():
            return OperationResult(False, "Launcher Windows non disponibile")
        preexisting = {item["pid"] for item in self.server_processes(managed_only=True)}
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stdout_log = self.log_dir / f"{mode.lower()}-{stamp}.out.log"
        stderr_log = self.log_dir / f"{mode.lower()}-{stamp}.err.log"
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
            "-Mode", mode, "-Port", str(port), "-BindHost", bind_host,
            "-ModelsDir", str(model.parent if mode == "Direct" else self.settings.models_dir),
            "-ModelsMax", str(models_max), "-CheckRuntime",
            "-BinPath", str(self.stack_manager.llama_bin_dir),
            "-GenieXDir", str(self.stack_manager.geniex_dir),
        ]
        if mode == "Direct":
            command += ["-Model", str(model)]
        try:
            with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open("w", encoding="utf-8") as stderr:
                child = subprocess.Popen(
                    command, cwd=self.workspace_dir, stdout=stdout, stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            return OperationResult(False, f"Avvio fallito: {exc}")
        record = OwnedServer(
            child.pid, None, mode, port, str(model or ""), time.time(), stdout_log, stderr_log,
        )
        self.owned[port] = record
        self.children[port] = child
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            online, _, _ = self.health()
            if online:
                candidates = [p for p in self.server_processes(managed_only=True) if p["pid"] not in preexisting]
                if candidates:
                    record.server_pid = candidates[-1]["pid"]
                    return OperationResult(True, f"Server {mode} pronto su {self.base_url}", record.server_pid, True)
            if child.poll() is not None:
                break
            time.sleep(0.25)
        detail = self._read_log(stderr_log) or self._read_log(stdout_log)
        candidates = [p for p in self.server_processes(managed_only=True) if p["pid"] not in preexisting]
        if candidates:
            record.server_pid = candidates[-1]["pid"]
        self._stop_record(record)
        self.owned.pop(port, None)
        self.children.pop(port, None)
        return OperationResult(False, "Server non disponibile entro il timeout. " + detail[-1500:])

    def _stop_pid(self, pid: int, force: bool = False) -> bool:
        if sys.platform != "win32":
            return False
        arguments = ["powershell", "-NoProfile", "-Command",
                     f"Stop-Process -Id {int(pid)} -ErrorAction Stop" + (" -Force" if force else "")]
        try:
            subprocess.run(arguments, capture_output=True, timeout=10, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _stop_record(self, record: OwnedServer) -> bool:
        pid = record.server_pid
        if pid is None:
            child = self.children.get(record.port)
            if child is not None and child.poll() is not None:
                return True
            pid = record.launcher_pid
        else:
            managed = {item["pid"] for item in self.server_processes(managed_only=True)}
            if pid not in managed:
                return True
        if not self._stop_pid(pid):
            return False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            running = {item["pid"] for item in self.server_processes(managed_only=False)}
            if pid not in running:
                return True
            time.sleep(0.25)
        if not self._stop_pid(pid, force=True):
            return False
        time.sleep(0.25)
        return pid not in {item["pid"] for item in self.server_processes(managed_only=False)}

    def stop(self, port: Optional[int] = None) -> OperationResult:
        ports = [int(port)] if port is not None else list(self.owned)
        if not ports:
            return OperationResult(True, "Nessun server avviato da questa sessione")
        failures = []
        stopped = []
        for item_port in ports:
            record = self.owned.get(item_port)
            if record is None:
                failures.append(f"porta {item_port}: non posseduto dalla sessione")
                continue
            if self._stop_record(record):
                stopped.append(str(item_port))
                self.owned.pop(item_port, None)
                self.children.pop(item_port, None)
            else:
                failures.append(f"porta {item_port}: arresto non confermato")
        messages = (["Arrestati server sulle porte " + ", ".join(stopped)] if stopped else []) + failures
        return OperationResult(not failures, "; ".join(messages))

    def shutdown(self, keep_running: bool) -> OperationResult:
        if keep_running:
            return OperationResult(True, "Server lasciati in background")
        return self.stop()

    def read_logs(self, port: int, errors_only: bool = False) -> str:
        record = self.owned.get(int(port))
        if record is None:
            return "Nessun log di sessione per questa porta"
        text = self._read_log(record.stderr_log) + "\n" + self._read_log(record.stdout_log)
        if errors_only:
            text = "\n".join(line for line in text.splitlines()
                             if any(word in line.lower() for word in ("error", "failed", "exception", "errore")))
        return text[-20000:] or "Log vuoto"

    @staticmethod
    def _read_log(path: Path) -> str:
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - 262144))
                return source.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
