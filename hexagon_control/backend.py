import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


class BackendError(RuntimeError):
    pass


class PowerShellBackend:
    def __init__(self, workspace_dir: Path, stack_manager):
        self.workspace_dir = Path(workspace_dir)
        self.stack_manager = stack_manager
        self.launcher = self.workspace_dir / "launch-hexagon.ps1"

    def _run_json(self, *arguments: str) -> Dict[str, Any]:
        if sys.platform != "win32":
            raise BackendError("Il backend launcher è disponibile su Windows ARM64.")
        if not self.launcher.is_file():
            raise BackendError(f"Launcher non trovato: {self.launcher}")
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(self.launcher),
            "-BinPath", str(self.stack_manager.llama_bin_dir),
            "-GenieXDir", str(self.stack_manager.geniex_dir),
            *map(str, arguments), "-Json",
        ]
        try:
            proc = subprocess.run(
                command, cwd=self.workspace_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendError(str(exc)) from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "Backend PowerShell fallito"
            raise BackendError(detail)
        try:
            result = json.loads(proc.stdout.strip().lstrip("\ufeff"))
        except ValueError as exc:
            raise BackendError(f"Risposta JSON non valida: {exc}") from exc
        if not isinstance(result, dict):
            raise BackendError("Risposta JSON inattesa dal launcher")
        return result

    def inventory(self, models_dir: Path) -> Dict[str, Any]:
        return self._run_json("-ListOnly", "-ModelsDir", str(models_dir))

    def direct_plan(self, model: Path, port: int = 18803,
                    bind_host: str = "127.0.0.1") -> Dict[str, Any]:
        return self._run_json(
            "-Mode", "Direct", "-DryRun", "-Model", str(model),
            "-ModelsDir", str(model.parent), "-Port", str(port), "-BindHost", bind_host,
        )

    def router_plan(self, models_dir: Path, port: int = 18803, models_max: int = 2,
                    bind_host: str = "127.0.0.1") -> Dict[str, Any]:
        return self._run_json(
            "-Mode", "Router", "-DryRun", "-ModelsDir", str(models_dir),
            "-Port", str(port), "-ModelsMax", str(models_max), "-BindHost", bind_host,
        )
