"""Contract tests with a fake tokenizer/server, never an NPU benchmark.

Run with POWERSHELL_EXE pointing to pwsh (Linux is supported for these tests).
The fake tokenizer maps characters to IDs; it does not model real GGUF tokens.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


POWERSHELL = os.environ.get("POWERSHELL_EXE") or shutil.which("pwsh") or shutil.which("powershell")
ROOT = Path(__file__).resolve().parents[1]


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.rsplit('/', 1)[-1] != 'slots':
            self.send_error(404)
            return
        data = json.dumps([{"id": 0, "n_ctx": 98304}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        endpoint = self.path.rsplit("/", 1)[-1]
        mode = self.path.strip("/").split("/")[0]
        if endpoint == "tokenize":
            value = {"tokens": [1] + [ord(c) for c in body["content"]]}
        elif endpoint == "apply-template":
            value = {"prompt": "USER\n" + body["messages"][0]["content"] + "\nASSISTANT\n"}
        elif endpoint == "completion":
            return self.completion(body, mode)
        else:
            self.send_error(404)
            return
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def completion(self, body, mode):
        if mode == "http-error":
            self.send_error(500, "fixture error")
            return
        prompt = "".join(chr(i) for i in body["prompt"] if i != 1)
        archive, query = prompt.split("Requested records:")
        values = dict(re.findall(r"Record ([0-9A-F]+) has access code ([0-9A-F]+)\.", archive))
        requested = re.findall(r"(early|middle|late|absent): record ([0-9A-F]+)", query)
        answer = json.dumps({name: values.get(key, "MISSING") for name, key in requested})
        with self.server.lock:
            self.server.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(value):
            self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
            self.wfile.flush()

        try:
            send({"content": "", "stop": False})
            if mode == "hang":
                time.sleep(2)
                return
            # Chunks are deliberately not tokens: client must use server counts.
            for text in (answer[:9], answer[9:]):
                send({"content": text, "stop": False, "tokens": [5]})
            if mode == "early-eof":
                return
            count = len(body["prompt"])
            send({
                "content": "", "stop": True, "tokens_evaluated": count,
                "tokens_predicted": 80, "tokens_cached": count + 79,
                "truncated": mode == "truncated", "generation_settings": {"temperature": 0},
                "timings": {"cache_n": 10 if mode == "cached" else 0,
                            "prompt_n": 100 if mode == "wrong-count" else count,
                            "predicted_n": 80},
            })
        except (BrokenPipeError, ConnectionResetError):
            pass


RUNNER = r"""
param($Source, $BaseUrl, $Artifacts)
$ErrorActionPreference = 'Stop'
. $Source -BinPath $Artifacts
Add-Type -AssemblyName System.Net.Http
$client = New-Object Net.Http.HttpClient
$client.Timeout = [TimeSpan]::FromSeconds(10)
function Get-LCEngineSnapshot { param($ServerId) return ,@() }
$process = [pscustomobject]@{ Id = 4242; HasExited = $false }
try {
    $case = New-LCCase $client ($BaseUrl + '/ok') 69632 12345
    $result = @{ Count = $case.Long.Tokens.Count; Positions = $case.Archive.Needles; Cases = @{} }
    foreach ($routeCase in @('ok', 'cached', 'truncated', 'wrong-count', 'early-eof', 'http-error', 'hang')) {
        try {
            $timeout = if ($routeCase -eq 'hang') { 1 } else { 20 }
            $completion = Invoke-LCCompletion $client ($BaseUrl + '/' + $routeCase) $case.Long 256 $process $timeout (Join-Path $Artifacts $routeCase)
            $result.Cases[$routeCase] = @{
                Processing = (Test-LCProcessing $completion $case.Long.Tokens.Count 256).Pass
                Retrieval = (Test-LCRetrieval $completion.Content $case.Archive).Pass
                FirstToken = $completion.FirstTokenMs; Error = $null
            }
        } catch { $result.Cases[$routeCase] = @{ Error = $_.Exception.Message } }
    }
    Write-LCJson $result (Join-Path $Artifacts 'test-results.json')
} finally { $client.Dispose() }
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is needed for HTTP contract tests")
class LongContextHttpTests(unittest.TestCase):
    def test_exact_counts_scoring_stream_failures_and_timeout(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.requests = []
        server.lock = threading.Lock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="longctx-contract-") as directory:
                artifacts = Path(directory)
                runner = artifacts / "runner.ps1"
                runner.write_text(RUNNER, encoding="utf-8")
                run = subprocess.run(
                    [POWERSHELL, "-NoLogo", "-NoProfile", "-File", str(runner),
                     "-Source", str(ROOT / "test-long-context-npu.ps1"),
                     "-BaseUrl", f"http://127.0.0.1:{server.server_port}", "-Artifacts", directory],
                    capture_output=True, text=True, timeout=90,
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                result = json.loads((artifacts / "test-results.json").read_text())
                self.assertGreater(result["Count"], 65536)
                self.assertLessEqual(result["Count"], 69632 + 256)
                self.assertGreater(result["Positions"][0]["TokensToPromptEnd"], 65536)
                self.assertGreater(result["Positions"][2]["TokenOffset"], 65536)
                self.assertTrue(result["Cases"]["ok"]["Processing"])
                self.assertTrue(result["Cases"]["ok"]["Retrieval"])
                self.assertIsNotNone(result["Cases"]["ok"]["FirstToken"])
                for mode in ("cached", "truncated", "wrong-count"):
                    self.assertFalse(result["Cases"][mode]["Processing"], mode)
                for mode in ("early-eof", "http-error", "hang"):
                    self.assertTrue(result["Cases"][mode]["Error"], mode)
                for request in server.requests:
                    self.assertFalse(request["cache_prompt"])
                    self.assertEqual(len(request["prompt"]), result["Count"])
                    self.assertNotIn("const", json.dumps(request["json_schema"]))
                    self.assertNotIn("enum", json.dumps(request["json_schema"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
