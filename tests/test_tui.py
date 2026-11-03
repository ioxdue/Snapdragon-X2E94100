"""Regressioni TUI senza server, modelli o hardware Qualcomm."""
import ctypes
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from prompt_toolkit.document import Document
from rich.console import Console

import tui


def sse(*events, done=True):
    payload = ''.join('data:' + json.dumps(event) + '\n\n' for event in events)
    return (payload + ('data: [DONE]\n\n' if done else '')).encode()


def delta(content=None, reasoning=None, finish=None):
    return {'choices': [{'delta': {'content': content, 'reasoning_content': reasoning},
                         'finish_reason': finish}]}


class TUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch('tui.PromptSession', return_value=None):
            self.app = tui.HexagonClawTUI(workspace_dir=self.root)
        self.app.http.close()
        self.output = io.StringIO()
        self.app.console = Console(file=self.output, width=180, color_system=None)
        self.requests = []
        self.addCleanup(self.app.close)

    def serve(self, payload, status=200):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, content=payload)
        self.app.http = httpx.Client(transport=httpx.MockTransport(handle))

    def test_reasoning_and_content_in_same_event(self):
        self.serve(sse(delta(content='risposta', reasoning='ragionamento'),
                       delta(finish='stop'), {'usage': {'completion_tokens': 13}}))
        self.app.stream_chat('ciao')
        self.assertEqual(self.app.messages, [{'role': 'user', 'content': 'ciao'},
                                            {'role': 'assistant', 'content': 'risposta'}])
        self.assertIn('ragionamento', self.output.getvalue())
        self.assertEqual(self.app.last_gen_tokens, 13)
        self.assertIsNone(self.app.last_gen_speed)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['max_tokens'], 2048)
        self.assertEqual(body['reasoning_effort'], 'none')
        self.assertEqual(body['stream_options'], {'include_usage': True})
        self.assertEqual(len(self.requests), 1)  # Nessun GET prima del POST.

    def test_fragmented_think_tags_and_multiple_blocks(self):
        text = '<think>segreto</think>ciao <think>altro</think>mondo'
        # Un carattere per evento: ogni delimitatore attraversa più eventi.
        self.serve(sse(*(delta(content=char) for char in text), delta(finish='stop')))
        self.app.stream_chat('ciao')
        self.assertEqual(self.app.messages[-1]['content'], 'ciao mondo')
        self.assertIsNone(self.app.last_gen_tokens)
        self.assertIsNone(self.app.last_gen_speed)
        self.assertNotIn('<think>', self.output.getvalue())

    def test_failure_leaves_existing_history_unchanged(self):
        previous = [{'role': 'user', 'content': 'prima'}, {'role': 'assistant', 'content': 'ok'}]
        self.app.messages = previous.copy()
        for payload, status in ((b'non disponibile', 503),
                                (b'data:broken\n\n', 200),
                                (sse(delta(content='parziale'), done=False), 200),
                                (sse({'error': {'message': 'errore'}}), 200)):
            with self.subTest(payload=payload):
                self.app.http.close()
                self.serve(payload, status)
                self.app.stream_chat('seconda')
                self.assertEqual(self.app.messages, previous)

    def test_interrupt_before_or_during_stream_does_not_save_partial_turn(self):
        class InterruptedStream(httpx.SyncByteStream):
            closed = False
            def __iter__(self):
                yield sse(delta(content='parziale'), done=False)
                raise KeyboardInterrupt
            def close(self):
                self.closed = True
        stream = InterruptedStream()
        self.app.http = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)))
        self.app.stream_chat('interrompi')
        self.assertEqual(self.app.messages, [])
        self.assertTrue(stream.closed)
        with patch.object(self.app.http, 'stream', side_effect=KeyboardInterrupt):
            self.app.stream_chat('prima della connessione')
        self.assertEqual(self.app.messages, [])

    def test_reasoning_only_does_not_create_empty_assistant(self):
        self.serve(sse(delta(reasoning='solo pensiero'), delta(finish='length')))
        self.app.stream_chat('ciao')
        self.assertEqual(self.app.messages, [])
        self.assertIn('limite', self.output.getvalue())
        self.assertIn('Ricevuto solo ragionamento', self.output.getvalue())

    def test_auto_reasoning_leaves_server_choice(self):
        self.app.handle_slash_command('/reasoning auto')
        self.serve(sse(delta(content='risposta'), delta(finish='stop')))
        self.app.stream_chat('ciao')
        self.assertNotIn('reasoning_effort', json.loads(self.requests[0].content))
        self.app.handle_slash_command('/reasoning off')
        self.app.stream_chat('ciao')
        self.assertEqual(json.loads(self.requests[1].content)['reasoning_effort'], 'none')

    def test_invalid_reasoning_command_keeps_current_setting(self):
        self.app.handle_slash_command('/reasoning auto')
        self.app.handle_slash_command('/reasoning invalid')
        self.assertEqual(self.app.reasoning, 'auto')
        self.assertIn('Uso: /reasoning off|auto', self.output.getvalue())

    def test_unclosed_think_block_and_whitespace_are_not_saved_as_final_answer(self):
        self.serve(sse(delta(content=' \n<think>solo pensiero'), delta(finish='length')))
        self.app.stream_chat('ciao')
        self.assertEqual(self.app.messages, [])
        self.assertIn('Ricevuto solo ragionamento', self.output.getvalue())

    def test_empty_final_answer_is_not_misdiagnosed_as_reasoning(self):
        self.serve(sse(delta(content=' \n'), delta(finish='length')))
        self.app.stream_chat('ciao')
        self.assertEqual(self.app.messages, [])
        self.assertIn('Nessuna risposta finale ricevuta', self.output.getvalue())
        self.assertNotIn('Ricevuto solo ragionamento', self.output.getvalue())

    def test_server_timings_are_used_without_counting_chunks(self):
        self.serve(sse(delta(content='più token in un solo frammento'),
                       {'choices': [], 'timings': {'predicted_n': 24, 'predicted_ms': 2000,
                                                   'predicted_per_second': 12.0}}))
        self.app.stream_chat('ciao')
        self.assertEqual((self.app.last_gen_tokens, self.app.last_gen_speed, self.app.last_gen_time),
                         (24, 12.0, 2.0))

    def test_benchmark_ttft_is_observed_not_prompt_processing_time(self):
        self.serve(sse(delta(content='risposta'), {'timings': {'prompt_ms': 17.0,
                       'predicted_n': 10, 'predicted_ms': 1000, 'predicted_per_second': 10.0}}))
        events = [delta(content='risposta'), {'timings': {'prompt_ms': 17.0,
                  'predicted_n': 10, 'predicted_ms': 1000, 'predicted_per_second': 10.0}}]
        with patch.object(self.app, '_chat_events', return_value=iter(events)), \
             patch('tui.time.perf_counter', side_effect=[0.0, 3.0, 5.0]):
            self.app.run_benchmark('10')
        result = self.output.getvalue()
        self.assertIn('3000.00 ms', result)
        self.assertIn('17.00 ms', result)
        self.assertEqual(self.app.messages, [])

    def test_benchmark_without_server_counts_does_not_invent_token_rate(self):
        self.serve(sse(delta(content='risposta')))
        self.app.run_benchmark('100')
        self.assertIn('N/D', self.output.getvalue())
        self.assertNotIn('tokens/sec', self.output.getvalue())

    def test_bad_benchmark_arguments_do_not_send_request(self):
        self.serve(b'')
        for value in ('-1', '0', 'abc', '1.5'):
            self.app.run_benchmark(value)
        self.assertEqual(self.requests, [])

    def test_remote_server_control_never_kills_local_processes(self):
        self.app.base_url = 'http://remote.example/v1'
        with patch.object(self.app.stack_mgr, 'stop_server') as stop, \
             patch.object(self.app.stack_mgr, 'start_server_router') as start:
            self.app.handle_server_command('stop')
            self.app.handle_server_command('restart')
        stop.assert_not_called()
        start.assert_not_called()

    def test_stack_commands_use_shared_updater_and_report_failures(self):
        with patch.object(self.app.stack_mgr, 'sync_runtime_from_geniex', return_value=(True, 'aligned', [])) as sync, \
             patch.object(self.app.stack_mgr, 'update_geniex_cli', return_value=(False, 'vendor failed')) as update:
            self.app.handle_stack_command('sync')
            self.app.handle_stack_command('update-llama')
            self.app.handle_stack_command('update-geniex')
        self.assertEqual(sync.call_count, 2)
        update.assert_called_once_with()
        self.assertIn('vendor failed', self.output.getvalue())

    def test_presets_reload_and_do_not_claim_unknown_hardware(self):
        (self.root / 'hexagon-models.ini').write_text(
            '\ufeffversion = 1\n[*]\nctx-size = 8192\n[model]\ndevice = GPUOpenCL\n', encoding='utf-8')
        self.serve(json.dumps({'data': [{'id': 'model'}]}).encode())
        self.app.refresh_models()
        self.assertEqual(self.app.get_model_info('model')['ctx'], '8192')
        self.assertEqual(self.app.get_model_info('model')['device'], 'GPUOpenCL')
        self.assertEqual(self.app.get_model_info('unknown')['device'], 'N/D')
        self.assertEqual(self.app.get_model_info('unknown')['ctx'], '0')
        (self.root / 'hexagon-models.ini').write_text('[model]\nctx-size = invalid\n', encoding='utf-8')
        self.app.refresh_models()
        self.assertEqual(self.app.get_model_info('model')['ctx'], '0')
        self.app.print_banner()
        self.app.show_tokens_info()
        self.assertNotIn('Rispettato', self.output.getvalue())

    def test_exact_model_match_is_case_insensitive(self):
        self.serve(json.dumps({'data': [{'id': 'MODEL'}, {'id': 'MODEL-extra'}]}).encode())
        self.app.switch_model('model')
        self.assertEqual(self.app.active_model, 'MODEL')

    def test_offline_windows_loopback_auto_starts_router(self):
        self.app.prompt_session = unittest.mock.Mock()
        self.app.prompt_session.prompt.return_value = '/exit'
        with patch.object(tui.sys, 'platform', 'win32'), \
             patch.object(self.app, 'refresh_models', side_effect=[(False, 0, []), (True, 0, [])]), \
             patch.object(self.app, 'handle_server_command') as control:
            self.app.run()
        control.assert_called_once_with('start')

    def test_offline_remote_endpoint_does_not_auto_start_local_router(self):
        self.app.base_url = 'http://remote.example/v1'
        self.app.prompt_session = unittest.mock.Mock()
        self.app.prompt_session.prompt.return_value = '/exit'
        with patch.object(tui.sys, 'platform', 'win32'), \
             patch.object(self.app, 'refresh_models', return_value=(False, 0, [])), \
             patch.object(self.app, 'handle_server_command') as control:
            self.app.run()
        control.assert_not_called()

    def test_maxtokens_slash_command(self):
        self.assertEqual(self.app.max_tokens, 2048)
        self.app.handle_slash_command("/maxtokens 4096")
        self.assertEqual(self.app.max_tokens, 4096)
        self.assertIn("4096", self.output.getvalue())

        self.app.handle_slash_command("/maxtokens")
        self.assertIn("attuale: 4096", self.output.getvalue())

        self.app.handle_slash_command("/maxtokens 0")
        self.assertEqual(self.app.max_tokens, 4096)
        self.assertIn("positivo", self.output.getvalue())

        self.app.handle_slash_command("/maxtokens invalido")
        self.assertEqual(self.app.max_tokens, 4096)
        self.assertIn("non valido", self.output.getvalue())

    def test_prompt_escapes_model_html(self):
        self.app.prompt_session = unittest.mock.Mock()
        self.app.active_model = 'model<&>'
        self.app.prompt_session.prompt.return_value = '/exit'
        with patch.object(self.app, 'refresh_models', return_value=(True, 0, [])):
            self.app.run()
        rendered_prompt = self.app.prompt_session.prompt.call_args.args[0]
        self.assertIn('model<&>', ''.join(text for style, text in rendered_prompt.__pt_formatted_text__()))


class ProtocolTests(unittest.TestCase):
    def test_sse_comments_multiline_and_done(self):
        lines = [': ping', 'event: message', 'data: {"choices":', 'data: []}', '',
                 'data:[DONE]', '']
        self.assertEqual(list(tui.iter_sse_events(lines)), [{'choices': []}, None])

    def test_sse_without_terminal_blank_line(self):
        self.assertEqual(list(tui.iter_sse_events(['data:{"usage":{}}'])), [{'usage': {}}])

    def test_nonfinite_metrics_are_unknown(self):
        self.assertEqual(tui.generation_metrics({'predicted_n': float('nan'),
                                               'predicted_ms': -1, 'predicted_per_second': float('inf')}, {}),
                         (None, None, None))

    def test_memory_structure_uses_windows_dword_width(self):
        self.assertEqual(ctypes.sizeof(tui.MEMORYSTATUSEX), 64)

    def test_trailing_space_completes_subcommands(self):
        completer = tui.OpenClawCompleter(lambda: ['model'])
        completions = list(completer.get_completions(Document('/stack '), None))
        self.assertEqual([entry.text for entry in completions], ['status', 'check', 'sync', 'update-geniex', 'update-llama'])
