"""Regressioni dell'inventario e del blocco aggiornamenti."""
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from rich.console import Console

import stack_manager as stack


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.genie = self.root / 'genie'
        self.source = self.genie / 'llama_cpp'
        self.target = self.root / 'bin'
        self.source.mkdir(parents=True)
        self.target.mkdir()
        self.manager = stack.StackManager(self.genie, self.target, self.root)
        process_check = patch.object(self.manager, 'is_server_running', return_value=False)
        process_check.start()
        self.addCleanup(process_check.stop)

    def test_empty_or_directory_only_runtime_is_not_aligned(self):
        self.assertFalse(self.manager.get_stack_status()['all_files_aligned'])
        (self.source / 'subdirectory').mkdir()
        self.assertFalse(self.manager.get_stack_status()['all_files_aligned'])

    def test_size_difference_avoids_hashing(self):
        (self.source / 'runtime.dll').write_bytes(b'new')
        (self.target / 'runtime.dll').write_bytes(b'old version')
        with patch('stack_manager.calculate_sha256') as digest:
            result = self.manager.get_stack_status()
        digest.assert_not_called()
        self.assertEqual(result['mismatched_files'], ['runtime.dll'])

    def test_hardlink_avoids_hashing(self):
        source = self.source / 'runtime.dll'
        source.write_bytes(b'same')
        os.link(source, self.target / source.name)
        with patch('stack_manager.calculate_sha256') as digest:
            result = self.manager.get_stack_status()
        digest.assert_not_called()
        self.assertTrue(result['all_files_aligned'])
        self.assertTrue(result['runtime_files'][0]['is_hardlink'])

    def test_identical_copies_are_not_reported_as_hardlinks(self):
        (self.source / 'runtime.dll').write_bytes(b'same')
        (self.target / 'runtime.dll').write_bytes(b'same')
        result = self.manager.get_stack_status()
        self.assertTrue(result['all_files_aligned'])
        self.assertFalse(result['runtime_files'][0]['is_hardlink'])
        output = io.StringIO()
        self.manager.render_stack_dashboard(Console(file=output, width=150))
        self.assertNotIn('16/16', output.getvalue())
        self.assertIn('0 hard-link', output.getvalue())
        self.assertNotIn('WHQL Attivo', output.getvalue())

    def test_critical_hash_reused_only_within_same_inspection(self):
        name = 'ggml-hexagon.dll'
        (self.source / name).write_bytes(b'same')
        (self.target / name).write_bytes(b'same')
        with patch('stack_manager.calculate_sha256', wraps=stack.calculate_sha256) as digest:
            self.manager.get_stack_status()
            self.assertEqual(digest.call_count, 2)
            (self.target / name).write_bytes(b'edit')
            self.assertFalse(self.manager.get_stack_status()['all_files_aligned'])
            self.assertEqual(digest.call_count, 4)

    def test_unreadable_runtime_is_reported_without_crash(self):
        (self.source / 'runtime.dll').write_bytes(b'same')
        (self.target / 'runtime.dll').write_bytes(b'same')
        with patch('stack_manager.calculate_sha256', side_effect=PermissionError('locked')):
            result = self.manager.get_stack_status()
        self.assertFalse(result['all_files_aligned'])
        self.assertTrue(result['errors'])

    def test_common_llama_version_and_geniex_version_formats(self):
        self.manager.geniex_exe.touch()
        self.manager.llama_server.touch()
        for version in ('version: 11938 (19cce6919)', 'version: b11938 commit 19cce6919'):
            def run(argv, **kwargs):
                text = ('GenieX CLI Version: 0.10.0\nQAIRT Runtime Version: 2.50\n'
                        'LlamaCPP Runtime Hash: 19cce6919abcdef\n') if 'geniex' in Path(argv[0]).name else version
                return subprocess.CompletedProcess(argv, 0, stdout='', stderr=text)
            with self.subTest(version=version), patch('stack_manager.subprocess.run', side_effect=run):
                result = self.manager.get_stack_status(include_runtime=False)
            self.assertEqual(result['llama_server_build'], 11938)
            self.assertEqual(result['llama_server_commit'], '19cce6919')
            self.assertTrue(result['commit_aligned'])

    def test_version_command_failure_does_not_produce_alignment(self):
        self.manager.llama_server.touch()
        with patch('stack_manager.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'version')):
            result = self.manager.get_stack_status(include_runtime=False)
        self.assertFalse(result['commit_aligned'])
        self.assertTrue(result['errors'])

    def test_sync_delegates_to_powershell_and_preserves_arguments(self):
        script = self.root / 'sync-geniex.ps1'
        script.write_text('# fixture')
        completed = subprocess.CompletedProcess([], 0, stdout='{"Message":"aligned","Files":["ggml.dll"],"Backup":"previous"}', stderr='')
        with patch.object(stack.sys, 'platform', 'win32'), \
             patch('stack_manager.subprocess.run', return_value=completed) as run:
            ok, message, files = self.manager.sync_runtime_from_geniex(auto_stop_server=False)
        self.assertTrue(ok)
        self.assertEqual(files, ['ggml.dll'])
        self.assertIn('previous', message)
        command = run.call_args.args[0]
        self.assertIn('-Apply', command)
        self.assertIn('-NoStopServer', command)
        self.assertEqual(command[command.index('-GenieXDir') + 1], str(self.genie))
        self.assertEqual(command[command.index('-BinPath') + 1], str(self.target))

    def test_vendor_update_failure_is_propagated(self):
        (self.root / 'sync-geniex.ps1').write_text('# fixture')
        completed = subprocess.CompletedProcess([], 1, stdout='{"Error":"vendor update failed"}', stderr='')
        with patch.object(stack.sys, 'platform', 'win32'), \
             patch('stack_manager.subprocess.run', return_value=completed) as run:
            ok, message = self.manager.update_geniex_cli()
        self.assertFalse(ok)
        self.assertEqual(message, 'vendor update failed')
        self.assertIn('-UpdateGenieX', run.call_args.args[0])

    def test_arbitrary_frontend_url_does_not_bypass_commit_selection(self):
        with patch('stack_manager.subprocess.run') as run:
            ok, _ = self.manager.update_llamacpp_frontend('https://example.test/latest.zip')
        self.assertFalse(ok)
        run.assert_not_called()

    def test_stop_server_scopes_stop_to_managed_directory(self):
        managed = str(self.manager.llama_server).replace("'", "''")
        outputs = iter([
            subprocess.CompletedProcess([], 0, stdout='1234\n', stderr=''),  # interrogazione PID
            subprocess.CompletedProcess([], 0, stdout='', stderr=''),         # Stop-Process
            subprocess.CompletedProcess([], 0, stdout='\n', stderr=''),      # verifica post-arresto
        ])
        calls = []
        def run(argv, **kwargs):
            command = argv[argv.index('-Command') + 1]
            calls.append(command)
            return next(outputs)
        with patch.object(stack.sys, 'platform', 'win32'), \
             patch('stack_manager.subprocess.run', side_effect=run):
            self.assertTrue(self.manager.stop_server())
        self.assertTrue(any('Get-CimInstance' in c and f"-eq '{managed}'" in c for c in calls))
        self.assertIn('Stop-Process -Force -ErrorAction Stop -Id 1234', calls)

    def test_stop_server_without_managed_processes_never_stops_anything(self):
        with patch.object(stack.sys, 'platform', 'win32'), \
             patch('stack_manager.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, stdout='', stderr='')) as run:
            self.assertTrue(self.manager.stop_server())
        run.assert_called_once()
        self.assertIn('Get-CimInstance', run.call_args.args[0][-1])

    def test_commit_prefixes_require_valid_hashes(self):
        for left, right in (('a', 'abcdef123'), ('N/A', 'N/A'), ('', ''), ('123456z', '123456z')):
            self.assertFalse(stack.same_commit(left, right))
        self.assertTrue(stack.same_commit('ABCDEF123', 'abcdef123456789'))

    def test_update_check_compares_build_numbers_and_does_not_run_updater(self):
        def handle(request):
            is_llama = 'llama.cpp' in str(request.url)
            return httpx.Response(200, json={'tag_name': 'b11938' if is_llama else 'v0.10.0',
                                            'published_at': None})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        status = {'geniex_version': '0.10.0', 'llama_server_commit': '19cce6919',
                  'llama_server_build': 11938}
        with patch.object(self.manager, 'get_stack_status', return_value=status) as inspect, \
             patch('stack_manager.httpx.Client', return_value=client):
            result = self.manager.check_updates()
        inspect.assert_called_once_with(include_runtime=False)
        self.assertIs(result['update_available_llamacpp'], False)
        self.assertIn('v0.10.0', result['geniex_update_status'])

    def test_update_failure_is_unknown_not_aligned(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        status = {'geniex_version': 'N/D', 'llama_server_commit': '', 'llama_server_build': None}
        with patch.object(self.manager, 'get_stack_status', return_value=status), \
             patch('stack_manager.httpx.Client', return_value=client):
            result = self.manager.check_updates()
        self.assertIsNone(result['update_available_llamacpp'])
        self.assertEqual(len(result['errors']), 2)

    def test_nondefault_geniex_installation_is_discovered_from_path(self):
        executable = self.genie / 'geniex.exe'
        executable.touch()
        with patch('stack_manager.DEFAULT_GENIEX_DIR', self.root / 'absent'), \
             patch('stack_manager.shutil.which', return_value=str(executable)):
            manager = stack.StackManager()
        self.assertEqual(manager.geniex_dir, self.genie)

    def test_default_workspace_is_checkout(self):
        self.assertEqual(stack.StackManager().workspace_dir, Path(stack.__file__).resolve().parent)
