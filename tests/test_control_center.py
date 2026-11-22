import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from hexagon_control.backend import PowerShellBackend
from hexagon_control.kilo import KiloController
from hexagon_control.links import LinkManager
from hexagon_control.models import ModelCatalog
from hexagon_control.presets import PresetError, PresetManager
from hexagon_control.processes import OwnedServer, ProcessController
from hexagon_control.settings import SettingsStore
from stack_manager import StackManager


VALID_INI = """version = 1
[*]
flash-attn = on
batch-size = 4096
ubatch-size = 512
[model]
model = C:/models/model.gguf
device = HTP0
ctx-size = 8192
"""


class ControlCenterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = SettingsStore(self.root / "settings.json")
        self.settings.data["models_dir"] = str(self.root / "models")
        self.settings.models_dir.mkdir()
        self.stack = StackManager(self.root / "genie", self.root / "bin", self.root)


class SettingsTests(ControlCenterTestCase):
    def test_paths_are_persisted_and_deduplicated(self):
        custom = self.root / "custom"
        custom.mkdir()
        self.assertTrue(self.settings.add_models_dir(custom))
        self.assertFalse(self.settings.add_models_dir(custom))
        loaded = SettingsStore(self.settings.path)
        self.assertEqual(loaded.models_dirs, [self.settings.models_dir, custom.resolve()])
        self.assertTrue(loaded.remove_models_dir(custom))
        self.assertFalse(loaded.remove_models_dir(custom))

    def test_corrupt_settings_fall_back_without_overwriting(self):
        self.settings.path.write_text("broken", encoding="utf-8")
        loaded = SettingsStore(self.settings.path)
        self.assertTrue(loaded.load_error)
        self.assertEqual(self.settings.path.read_text(encoding="utf-8"), "broken")


class BackendTests(ControlCenterTestCase):
    def test_json_inventory_uses_read_only_contract(self):
        (self.root / "launch-hexagon.ps1").write_text("# fixture", encoding="utf-8")
        backend = PowerShellBackend(self.root, self.stack)
        completed = subprocess.CompletedProcess([], 0, stdout='{"models": []}', stderr="")
        with patch("hexagon_control.backend.sys.platform", "win32"), \
             patch("hexagon_control.backend.subprocess.run", return_value=completed) as run:
            result = backend.inventory(self.settings.models_dir)
        self.assertEqual(result, {"models": []})
        command = run.call_args.args[0]
        self.assertIn("-ListOnly", command)
        self.assertIn("-Json", command)
        self.assertNotIn("-Apply", command)

    def test_router_plan_preserves_port_models_max_and_loopback(self):
        (self.root / "launch-hexagon.ps1").write_text("# fixture", encoding="utf-8")
        backend = PowerShellBackend(self.root, self.stack)
        completed = subprocess.CompletedProcess([], 0, stdout='{"ini_content": ""}', stderr="")
        with patch("hexagon_control.backend.sys.platform", "win32"), \
             patch("hexagon_control.backend.subprocess.run", return_value=completed) as run:
            backend.router_plan(self.settings.models_dir, 19000, 3)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-Port") + 1], "19000")
        self.assertEqual(command[command.index("-ModelsMax") + 1], "3")
        self.assertEqual(command[command.index("-BindHost") + 1], "127.0.0.1")
        self.assertIn("-DryRun", command)


class PresetTests(ControlCenterTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "hexagon-models.ini"
        self.path.write_text(VALID_INI, encoding="utf-8")
        self.manager = PresetManager(self.path, MagicMock())

    def test_update_presets_preserves_other_sections_and_writes_without_bom(self):
        other = VALID_INI + "[other]\nctx-size = 4096\n"
        self.path.write_text(other, encoding="utf-8")
        backup = self.manager.update_parameter("model", "ctx-size", "16384")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("ctx-size = 16384", text)
        self.assertIn("[other]\nctx-size = 4096", text)
        self.assertTrue(backup.is_file())
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_invalid_update_does_not_modify_file(self):
        before = self.path.read_bytes()
        with self.assertRaises(PresetError):
            self.manager.update_parameter("model", "ubatch-size", "8192")
        self.assertEqual(self.path.read_bytes(), before)

    def test_tensor_split_requires_matching_devices_and_100_percent(self):
        invalid = VALID_INI.replace("device = HTP0", "device = HTP0,GPUOpenCL\ntensor-split = 80,10")
        self.assertTrue(any("sommare" in error for error in self.manager.validate(invalid)))
        invalid = invalid.replace("80,10", "80,20,10")
        self.assertTrue(any("cardinalità" in error for error in self.manager.validate(invalid)))

    def test_router_preview_does_not_write_ini(self):
        generated = VALID_INI.replace("8192", "32768")
        self.manager.backend.router_plan.return_value = {"ini_content": generated, "models": []}
        before = self.path.read_bytes()
        plan, diff = self.manager.preview_router(self.settings.models_dir)
        self.assertIn("32768", diff)
        self.assertEqual(plan["models"], [])
        self.assertEqual(self.path.read_bytes(), before)


class LinkTests(ControlCenterTestCase):
    def test_hardlink_collision_never_overwrites_and_removal_preserves_source(self):
        source_dir = self.settings.models_dir / "source"
        source_dir.mkdir()
        source = source_dir / "model.gguf"
        source.write_bytes(b"model")
        destination = self.settings.models_dir / "model.gguf"
        manager = LinkManager(self.settings)
        result = manager.create(source, destination)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(source.samefile(destination))
        destination.write_bytes(b"collision")
        collision = manager.create(source_dir / "other.gguf", destination)
        self.assertFalse(collision.ok)
        self.assertEqual(destination.read_bytes(), b"collision")
        removed = manager.remove(destination)
        self.assertTrue(removed.ok, removed.message)
        self.assertTrue(source.is_file())
        self.assertEqual(source.read_bytes(), b"collision")

    def test_regular_file_and_external_path_are_not_deleted(self):
        regular = self.settings.models_dir / "regular.gguf"
        regular.write_bytes(b"data")
        manager = LinkManager(self.settings)
        self.assertFalse(manager.remove(regular).ok)
        self.assertTrue(regular.exists())
        external = self.root / "external.gguf"
        os.link(regular, external)
        self.assertFalse(manager.remove(external).ok)
        self.assertTrue(external.exists())


class ModelTests(ControlCenterTestCase):
    def test_gguf_header_and_sha256(self):
        path = self.settings.models_dir / "model.gguf"
        path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 7, 11) + b"payload")
        self.assertEqual(ModelCatalog.read_header(path),
                         {"version": 3, "tensor_count": 7, "metadata_count": 11})
        self.assertEqual(len(ModelCatalog.sha256(path)), 64)
        path.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            ModelCatalog.read_header(path)

    def test_tool_calling_is_marked_verified_only_from_real_response_shape(self):
        catalog = ModelCatalog(MagicMock(), self.settings, "http://127.0.0.1:18803/v1")
        model_path = self.settings.models_dir / "model.gguf"
        response = httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]},
                                  request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"))
        client = MagicMock()
        client.__enter__.return_value.post.return_value = response
        with patch("hexagon_control.models.httpx.Client", return_value=client):
            result = catalog.verify_tool_call("model", model_path)
        self.assertTrue(result.ok)
        self.assertEqual(self.settings.data["tool_calling"][str(model_path)]["status"], "verificato")


class ProcessTests(ControlCenterTestCase):
    def setUp(self):
        super().setUp()
        self.processes = ProcessController(
            self.root, self.stack, self.settings, "http://127.0.0.1:18803/v1")
        self.addCleanup(self.processes.close)

    def test_non_loopback_requires_explicit_permission(self):
        with self.assertRaises(ValueError):
            self.processes.validate_bind("0.0.0.0")
        self.processes.validate_bind("0.0.0.0", allow_non_loopback=True)
        result = self.processes.start("Router", bind_host="0.0.0.0")
        self.assertFalse(result.ok)
        self.assertIn("conferma esplicita", result.message)

    def test_start_uses_check_runtime_and_tracks_new_server_pid(self):
        (self.root / "launch-hexagon.ps1").write_text("# fixture", encoding="utf-8")
        child = MagicMock(pid=10)
        child.poll.return_value = None
        with patch("hexagon_control.processes.sys.platform", "win32"), \
             patch.object(self.processes, "health", side_effect=[(False, 0, []), (True, 1, ["model"])]), \
             patch.object(self.processes, "server_processes", side_effect=[[], [{"pid": 42}]]), \
             patch("hexagon_control.processes.subprocess.Popen", return_value=child) as popen, \
             patch("hexagon_control.processes.time.sleep"):
            result = self.processes.start("Router")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.pid, 42)
        self.assertEqual(self.processes.owned[18803].server_pid, 42)
        command = popen.call_args.args[0]
        self.assertIn("-CheckRuntime", command)
        self.assertNotIn("-Apply", command)
        self.assertIn("Bypass", command)

    def test_stop_targets_only_owned_server_pid(self):
        record = OwnedServer(10, 42, "Router", 18803, "", 0,
                             self.root / "out.log", self.root / "err.log")
        self.processes.owned[18803] = record
        with patch.object(self.processes, "_stop_pid", return_value=True) as stop, \
             patch.object(self.processes, "server_processes", side_effect=[[{"pid": 42}], []]):
            result = self.processes.stop(18803)
        self.assertTrue(result.ok)
        stop.assert_called_once_with(42)
        self.assertFalse(self.processes.owned)

    def test_keep_running_never_calls_stop(self):
        with patch.object(self.processes, "stop") as stop:
            result = self.processes.shutdown(keep_running=True)
        self.assertTrue(result.ok)
        stop.assert_not_called()


class KiloTests(ControlCenterTestCase):
    def test_prepare_uses_selected_models_and_validates_local_config(self):
        processes = MagicMock()
        processes.health.return_value = (True, 1, ["main", "small"])
        processes.base_url = "http://127.0.0.1:18803/v1"
        controller = KiloController(self.root, processes, self.settings)
        result = controller.prepare("main", "small")
        self.assertTrue(result.ok, result.message)
        config = json.loads((self.root / "kilo.json").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "hexagon/main")
        self.assertEqual(config["small_model"], "hexagon/small")
        processes.start.assert_not_called()

    def test_failed_router_start_does_not_write_config(self):
        processes = MagicMock()
        processes.health.return_value = (False, 0, [])
        processes.start.return_value.ok = False
        processes.start.return_value.message = "offline"
        controller = KiloController(self.root, processes, self.settings)
        result = controller.prepare("main", "small")
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "kilo.json").exists())


class DiagnosticsUsageTests(ControlCenterTestCase):
    def controller(self):
        from hexagon_control.diagnostics import DiagnosticsController
        return DiagnosticsController(self.root, MagicMock(), self.settings, MagicMock())

    def test_accelerator_usage_parses_gpu_and_cpu_counters(self):
        completed = subprocess.CompletedProcess([], 0, stdout=(
            "GPUMAX|0x00000000_0x00012f23|3d|93.8\n"
            "GPUMAX|0x00000000_0x00013259|compute|0.0\n"
            "CPUSUM|0.0\n"), stderr="")
        with patch('hexagon_control.diagnostics.subprocess.run', return_value=completed):
            label = self.controller().accelerator_usage()
        self.assertIn("GPU 94%", label)
        self.assertIn("0.0 core", label)
        self.assertIn("NPU n/d", label)

    def test_accelerator_usage_idle_hint_and_failure_path(self):
        completed = subprocess.CompletedProcess([], 0, stdout=(
            "GPUMAX|0x00000000_0x00012f23|3d|5.0\nCPUSUM|0.0\n"), stderr="")
        with patch('hexagon_control.diagnostics.subprocess.run', return_value=completed):
            self.assertIn("fermo", self.controller().accelerator_usage())
        failing = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with patch('hexagon_control.diagnostics.subprocess.run', return_value=failing):
            self.assertIn("N/D", self.controller().accelerator_usage())


class NavigationTests(ControlCenterTestCase):
    def test_all_nine_areas_and_chat_are_callable(self):
        from hexagon_control.ui.app import ControlCenter
        processes = MagicMock()
        processes.base_url = "http://127.0.0.1:18803/v1"
        app = ControlCenter(processes, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                            MagicMock(), MagicMock(), self.settings)
        for key in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "c", "l"):
            self.assertTrue(callable(app.actions[key]), key)


if __name__ == "__main__":
    unittest.main()
