#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from stack_manager import DEFAULT_WORKSPACE, StackManager
from hexagon_control.backend import PowerShellBackend
from hexagon_control.diagnostics import DiagnosticsController
from hexagon_control.kilo import KiloController
from hexagon_control.links import LinkManager
from hexagon_control.models import ModelCatalog
from hexagon_control.presets import PresetManager
from hexagon_control.processes import ProcessController
from hexagon_control.settings import SettingsStore
from hexagon_control.ui.app import ControlCenter
from hexagon_control.updates import UpdateController


def main():
    parser = argparse.ArgumentParser(description="Centro di controllo locale Hexagon")
    parser.add_argument("--url", default="http://127.0.0.1:18803/v1")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Il centro di controllo accetta solo endpoint HTTP loopback")
    workspace = args.workspace.resolve()
    settings = SettingsStore()
    stack = StackManager(workspace_dir=workspace)
    processes = ProcessController(workspace, stack, settings, args.url)
    backend = PowerShellBackend(workspace, stack)
    catalog = ModelCatalog(backend, settings, processes.base_url)
    presets = PresetManager(workspace / "hexagon-models.ini", backend)
    links = LinkManager(settings)
    diagnostics = DiagnosticsController(workspace, stack, settings, processes)
    kilo = KiloController(workspace, processes, settings)
    updates = UpdateController(stack)
    app = ControlCenter(processes, catalog, presets, links, diagnostics, kilo, updates, settings)
    try:
        app.run()
    finally:
        processes.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
