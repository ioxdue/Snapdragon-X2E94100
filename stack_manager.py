#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ispezione e aggiornamento dello stack GenieX / llama.cpp.

La presenza e l'identità dei file non verificano firma, ABI o offload reale.
Installazione e aggiornamento delegano a sync-geniex.ps1, condiviso con il launcher.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

USER_PROFILE = Path(os.environ.get("USERPROFILE") or Path.home())
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or USER_PROFILE / "AppData" / "Local")
DEFAULT_GENIEX_DIR = LOCAL_APPDATA / "GenieX CLI"
DEFAULT_LLAMA_BIN_DIR = USER_PROFILE / "llama-cpp" / "llama-bin-win-cpu-arm64"
DEFAULT_WORKSPACE = Path(__file__).resolve().parent


def calculate_sha256(path: Path) -> str:
    """Hash del contenuto corrente; gli errori di lettura restano espliciti."""
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def same_commit(left: str, right: str) -> bool:
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{7,40}", left)
        and re.fullmatch(r"[0-9a-fA-F]{7,40}", right)
        and (left.lower().startswith(right.lower()) or right.lower().startswith(left.lower()))
    )


class StackManager:
    def __init__(self, geniex_dir: Optional[Path] = None,
                 llama_bin_dir: Optional[Path] = None, workspace_dir: Optional[Path] = None):
        # La ricerca di geniex.exe nel PATH vale solo per il percorso predefinito:
        # una directory esplicita mancante segnala un errore di configurazione, non un fallback.
        self.geniex_dir = Path(geniex_dir or DEFAULT_GENIEX_DIR)
        if geniex_dir is None and not (self.geniex_dir / "geniex.exe").is_file():
            executable = shutil.which("geniex.exe")
            if executable:
                self.geniex_dir = Path(executable).parent
        self.geniex_exe = self.geniex_dir / "geniex.exe"
        self.geniex_llama_dir = self.geniex_dir / "llama_cpp"
        self.llama_bin_dir = Path(llama_bin_dir or DEFAULT_LLAMA_BIN_DIR)
        self.llama_server = self.llama_bin_dir / "llama-server.exe"
        self.workspace_dir = Path(workspace_dir or DEFAULT_WORKSPACE)

    def is_server_running(self) -> Optional[bool]:
        """None indica che il controllo processi non è disponibile."""
        if sys.platform == "win32":
            try:
                proc = subprocess.run(
                    ["tasklist", "/fi", "imagename eq llama-server.exe", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, errors="replace", timeout=5, check=True,
                )
                return "llama-server.exe" in proc.stdout.lower()
            except (OSError, subprocess.SubprocessError):
                pass
        return None

    def managed_server_pids(self) -> List[int]:
        """PID dei llama-server.exe avviati dalla directory gestita; [] se non disponibile."""
        if sys.platform != "win32":
            return []
        path = str(self.llama_server).replace("'", "''")
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" -ErrorAction Stop | "
            f"Where-Object {{ $_.ExecutablePath -eq '{path}' }} | ForEach-Object {{ $_.ProcessId }}"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, errors="replace", timeout=10, check=True,
            )
            return [int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def stop_server(self) -> bool:
        """Arresto esplicito dei soli llama-server.exe della directory gestita.

        Gli altri llama-server.exe locali, non installati da questo stack, restano attivi.
        """
        pids = self.managed_server_pids()
        if sys.platform != "win32":
            return False
        if not pids:
            return True
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Force -ErrorAction Stop -Id {','.join(map(str, pids))}"],
                capture_output=True, timeout=10, check=True,
            )
            for _ in range(10):
                if not self.managed_server_pids():
                    return True
                time.sleep(0.3)
        except (OSError, subprocess.SubprocessError):
            pass
        return False

    def start_server_router(self, port: int = 18803, bind_host: str = "127.0.0.1") -> bool:
        """Avvia il launcher del checkout corrente; la TUI verifica poi l'HTTP."""
        script = self.workspace_dir / "launch-hexagon.ps1"
        if sys.platform != "win32" or not script.is_file():
            return False
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Mode", "Router", "-Port", str(port), "-BindHost", bind_host,
                 "-BinPath", str(self.llama_bin_dir), "-GenieXDir", str(self.geniex_dir)],
                cwd=self.workspace_dir, creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return True
        except OSError:
            return False

    def _catalog_signature_status(self) -> str:
        """Stato della firma Authenticode di libggml-htp.cat; 'N/D' se non verificabile.

        Lo stato della firma non dimostra l'attendibilita' del certificato ne' la
        compatibilita' degli artefatti; serve solo a rendere esplicito cio' che il
        file system locale dichiara.
        """
        if sys.platform != "win32":
            return "N/D"
        path = self.llama_bin_dir / "libggml-htp.cat"
        if not path.is_file():
            return "N/D"
        script = f"(Get-AuthenticodeSignature -LiteralPath '{str(path).replace(chr(39), chr(39) * 2)}').Status"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, errors="replace", timeout=15, check=True,
            )
            lines = proc.stdout.strip().splitlines()
            if not lines:
                return "N/D"
            status = lines[-1].strip()
            return {"Valid": "Valida", "NotSigned": "Non firmata"}.get(status, status or "N/D")
        except (OSError, subprocess.SubprocessError):
            return "N/D"

    def get_stack_status(self, include_runtime: bool = True) -> Dict[str, Any]:
        status = {
            "geniex_installed": self.geniex_exe.is_file(), "geniex_version": "Non rilevato",
            "qairt_version": "N/A", "geniex_llama_hash": "",
            "llama_server_installed": self.llama_server.is_file(),
            "llama_server_version": "Non rilevato", "llama_server_commit": "",
            "llama_server_build": None, "commit_aligned": False, "runtime_files": [],
            "all_files_aligned": False, "mismatched_files": [], "missing_files": [],
            "npu_artifacts": {}, "server_running": self.is_server_running(), "errors": [],
        }
        for exe, args, prefix in (
            (self.geniex_exe, ["--version", "--skip-update"], "geniex"),
            (self.llama_server, ["--version"], "llama_server"),
        ):
            if not exe.is_file():
                continue
            try:
                env = os.environ.copy()
                if prefix == "llama_server":
                    env["ADSP_LIBRARY_PATH"] = str(self.llama_bin_dir)
                proc = subprocess.run(
                    [str(exe), *args], cwd=exe.parent, env=env, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=5, check=True,
                )
                output = proc.stdout + "\n" + proc.stderr
                if prefix == "geniex":
                    for label, key in (("GenieX CLI Version", "geniex_version"),
                                       ("QAIRT Runtime Version", "qairt_version"),
                                       ("LlamaCPP Runtime Hash", "geniex_llama_hash")):
                        match = re.search(re.escape(label) + r":\s*([^\r\n]+)", output)
                        if match:
                            status[key] = match.group(1).strip()
                else:
                    match = re.search(r"\bversion:\s*([^\r\n]+)", output, re.I)
                    if match:
                        version = match.group(1).strip()
                        status["llama_server_version"] = version
                        build = re.match(r"b?(\d+)\b", version)
                        if build:
                            status["llama_server_build"] = int(build.group(1))
                        commit = re.search(r"\bcommit\s+([0-9a-fA-F]{7,40})\b|\(([0-9a-fA-F]{7,40})\)", version)
                        if commit:
                            status["llama_server_commit"] = commit.group(1) or commit.group(2)
            except (OSError, subprocess.SubprocessError) as exc:
                status[prefix + "_version"] = "Errore lettura"
                status["errors"].append(f"{exe.name}: {exc}")

        status["commit_aligned"] = same_commit(status["geniex_llama_hash"], status["llama_server_commit"])
        if not include_runtime:
            return status

        # Cache limitata a questa ispezione: niente hash persistenti o basati sui timestamp.
        hashes = {}

        def digest(path):
            if path not in hashes:
                hashes[path] = calculate_sha256(path)
            return hashes[path]

        try:
            sources = sorted(p for p in self.geniex_llama_dir.iterdir() if p.is_file())
            for source in sources:
                target = self.llama_bin_dir / source.name
                info = {
                    "name": source.name, "genie_size": source.stat().st_size,
                    "target_exists": target.is_file(), "target_size": 0,
                    "match": False, "is_hardlink": False,
                }
                if target.is_file():
                    try:
                        info["target_size"] = target.stat().st_size
                        info["is_hardlink"] = source.samefile(target)
                        info["match"] = info["is_hardlink"] or (
                            info["genie_size"] == info["target_size"] and digest(source) == digest(target)
                        )
                    except OSError as exc:
                        status["errors"].append(f"{source.name}: {exc}")
                    if not info["match"]:
                        status["mismatched_files"].append(source.name)
                else:
                    status["missing_files"].append(source.name)
                status["runtime_files"].append(info)
            status["all_files_aligned"] = bool(sources) and all(f["match"] for f in status["runtime_files"])
        except OSError as exc:
            status["errors"].append(f"Inventario runtime non disponibile: {exc}")

        for name in ("libggml-htp-v81.so", "libggml-htp.cat", "ggml-hexagon.dll", "ggml-opencl.dll"):
            path = self.llama_bin_dir / name
            artifact = {"present": path.is_file(), "size": 0, "sha256": "N/A", "signature": "N/D"}
            if name == "libggml-htp.cat" and artifact["present"]:
                artifact["signature"] = self._catalog_signature_status()
            if artifact["present"]:
                try:
                    artifact.update(size=path.stat().st_size, sha256=digest(path)[:12] + "...")
                except OSError as exc:
                    status["errors"].append(f"{name}: {exc}")
            status["npu_artifacts"][name] = artifact
        return status

    def check_updates(self) -> Dict[str, Any]:
        """Consulta release pubblicate senza eseguire updater o rileggere tutti gli hash."""
        current = self.get_stack_status(include_runtime=False)
        result = {
            "current_geniex": current["geniex_version"],
            "current_llamacpp_commit": current["llama_server_commit"] or "N/D",
            "latest_llamacpp_build": "N/D", "latest_llamacpp_published": "",
            "latest_llamacpp_url": "", "update_available_llamacpp": None,
            "geniex_update_status": "Verifica non disponibile",
            "safe_update_advisory": (
                "Controllo in sola lettura. La pubblicazione di una release non dimostra "
                "la compatibilità con lo stack locale. /stack sync allinea al commit GenieX; "
                "/stack update-geniex aggiorna prima GenieX e poi il runtime."
            ), "errors": [],
        }
        try:
            client = httpx.Client(timeout=6.0, follow_redirects=True)
        except (ImportError, ValueError) as exc:
            result["errors"].append(f"Configurazione HTTP non disponibile: {exc}")
            return result
        with client:
            for repo in ("ggml-org/llama.cpp", "qualcomm/GenieX"):
                try:
                    resp = client.get(f"https://api.github.com/repos/{repo}/releases/latest")
                    resp.raise_for_status()
                    release = resp.json()
                    tag = release["tag_name"]
                    if repo == "ggml-org/llama.cpp":
                        result["latest_llamacpp_build"] = tag
                        result["latest_llamacpp_published"] = (release.get("published_at") or "")[:10]
                        result["latest_llamacpp_url"] = release.get("html_url", "")
                        build = re.fullmatch(r"b(\d+)", tag)
                        if build and current["llama_server_build"] is not None:
                            result["update_available_llamacpp"] = int(build.group(1)) > current["llama_server_build"]
                    else:
                        result["geniex_update_status"] = f"Release pubblicata: {tag}; locale: {current['geniex_version']}"
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    result["errors"].append(f"{repo}: {exc}")
        return result

    def _run_runtime_update(self, *options) -> Tuple[bool, Dict[str, Any]]:
        if sys.platform != "win32":
            return False, {"Error": "Aggiornamento disponibile su Windows ARM64."}
        script = self.workspace_dir / "sync-geniex.ps1"
        if not script.is_file():
            return False, {"Error": f"Script non trovato: {script}"}
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-BinPath", str(self.llama_bin_dir), "-GenieXDir", str(self.geniex_dir),
                 "-Apply", "-Json", *options],
                cwd=self.workspace_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900,
            )
            # Una diagnostica PowerShell puo' precedere l'ultima riga JSON.
            result = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
            if proc.returncode != 0:
                return False, {"Error": result.get("Error") or proc.stderr.strip() or "Aggiornamento fallito"}
            if not isinstance(result.get("Message"), str):
                return False, {"Error": "Risposta del sincronizzatore non valida."}
            return True, result
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
            return False, {"Error": str(exc)}

    def sync_runtime_from_geniex(self, auto_stop_server: bool = True) -> Tuple[bool, str, List[str]]:
        options = [] if auto_stop_server else ["-NoStopServer"]
        ok, result = self._run_runtime_update(*options)
        message = result.get("Message", "") if ok else result.get("Error", "")
        if ok and result.get("Backup"):
            message += f" Backup: {result['Backup']}"
        return ok, message, result.get("Files", []) if ok else []

    def update_llamacpp_frontend(self, download_url: Optional[str] = None,
                                auto_stop_server: bool = True) -> Tuple[bool, str]:
        if download_url:
            return False, "Il frontend viene selezionato dal commit GenieX; usare /stack sync."
        ok, message, _ = self.sync_runtime_from_geniex(auto_stop_server)
        return ok, message

    def update_geniex_cli(self) -> Tuple[bool, str]:
        ok, result = self._run_runtime_update("-UpdateGenieX")
        return ok, result.get("Message", "") if ok else result.get("Error", "")

    def render_stack_dashboard(self, console: Console):
        st = self.get_stack_status()
        table = Table(title="🐉 Inventario Stack GenieX / llama.cpp", border_style="cyan")
        table.add_column("Componente", style="bold cyan")
        table.add_column("Versione / Dettaglio")
        table.add_column("Esito osservato")
        table.add_row("GenieX CLI", Text(f"{st['geniex_version']} (QAIRT {st['qairt_version']})"),
                      "Presente" if st["geniex_installed"] else "Mancante")
        table.add_row("llama.cpp Server", Text(st["llama_server_version"]),
                      "Presente" if st["llama_server_installed"] else "Mancante")
        table.add_row("Commit", Text(f"GenieX: {st['geniex_llama_hash'] or 'N/D'}; "
                                     f"llama.cpp: {st['llama_server_commit'] or 'N/D'}"),
                      "Coincidono" if st["commit_aligned"] else "Differenti o non rilevati")
        files = st["runtime_files"]
        table.add_row("Runtime", f"{sum(f['match'] for f in files)}/{len(files)} file identici; "
                      f"{sum(f['is_hardlink'] for f in files)} hard-link",
                      "Identità verificata" if st["all_files_aligned"] else "Differenze o inventario incompleto")
        for name, artifact in st["npu_artifacts"].items():
            detail = artifact["sha256"]
            if artifact.get("signature") and artifact["signature"] != "N/D":
                detail = f"{detail}; firma: {artifact['signature']}"
            table.add_row(name, detail, "Presente" if artifact["present"] else "Mancante")
        running = st["server_running"]
        table.add_row("Processi llama-server", "Controllo locale per nome",
                      "N/D" if running is None else "In esecuzione" if running else "Fermi")
        console.print(table)
        console.print("[dim]File presenti e commit uguali non verificano firma, ABI o offload reale.[/dim]")
        for error in st["errors"]:
            console.print(Text(error, style="yellow"))

    def render_updates_dashboard(self, console: Console):
        up = self.check_updates()
        available = up["update_available_llamacpp"]
        state = "Non determinabile" if available is None else "Build più recente pubblicata" if available else "Nessuna build più recente"
        table = Table(title="🐉 Release pubblicate", border_style="cyan")
        table.add_column("Software")
        table.add_column("Versione locale")
        table.add_column("Release / Esito")
        table.add_row("llama.cpp", Text(up["current_llamacpp_commit"]),
                      Text(f"{up['latest_llamacpp_build']} ({up['latest_llamacpp_published']}): {state}"))
        table.add_row("GenieX", Text(up["current_geniex"]), Text(up["geniex_update_status"]))
        console.print(table)
        console.print(Panel(Text(up["safe_update_advisory"]), border_style="yellow"))
        for error in up["errors"]:
            console.print(Text(error, style="yellow"))
