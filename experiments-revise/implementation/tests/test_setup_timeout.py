




from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from msi_supplement import core, runner, service


SUITE = Path(__file__).resolve().parents[1]


class SetupTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="timeout-regression-not-pi-", dir=SUITE)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "provider.token"
        service.create_token(self.token_file)
        self.token = service.read_token(self.token_file)
        self.server = service.ExperimentServer(("127.0.0.1", 0), self.root / "provider", self.token)
        
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def client(self, **kwargs):
        client = service.Client(self.url, self.token, **kwargs)
        self.clients.append(client)
        return client

    @staticmethod
    def create_request():
        return {"mode": "leaf", "payloads_b64": [base64.b64encode(b"opaque record").decode()]}

    def test_slow_setup_reuses_health_connection_then_restores_query_timeout(self):
        client = self.client(timeout=0.1, setup_timeout=1.5)
        self.assertEqual(client.call("/health", {})["status"], "ok")
        connection, sock = client.conn, client.conn.sock
        genuine_create = core.create_case

        def slow_create(*args, **kwargs):
            time.sleep(0.25)
            return genuine_create(*args, **kwargs)

        with patch.object(service.core, "create_case", side_effect=slow_create):
            created = client.call("/create", self.create_request())
        self.assertIs(client.conn, connection)
        self.assertIs(client.conn.sock, sock)
        self.assertAlmostEqual(sock.gettimeout(), 1.5)
        package = client.call("/query", {"case_id": created["case_id"], "position": 1})
        self.assertTrue(core.verify_response(created["descriptor"], package["response"]))
        self.assertIs(client.conn.sock, sock)
        self.assertAlmostEqual(sock.gettimeout(), 0.1)
        self.assertAlmostEqual(client.conn.timeout, 0.1)

    def test_slow_query_keeps_short_deadline_and_next_connection_recovers(self):
        client = self.client(timeout=0.1, setup_timeout=1.5)
        created = client.call("/create", self.create_request())
        genuine_query = core.query_store

        def slow_query(*args, **kwargs):
            time.sleep(0.25)
            return genuine_query(*args, **kwargs)

        with patch.object(service.core, "query_store", side_effect=slow_query):
            with self.assertRaisesRegex(TimeoutError, r"request /query:.*0\.1s"):
                client.call("/query", {"case_id": created["case_id"], "position": 1})
            self.assertIsNone(client.conn)
            self.assertEqual(client.call("/health", {})["status"], "ok")
            self.assertAlmostEqual(client.conn.sock.gettimeout(), 0.1)

    def test_setup_timeout_error_identifies_preparation_and_closes_connection(self):
        client = self.client(timeout=1.5, setup_timeout=0.1)
        self.assertEqual(client.call("/health", {})["status"], "ok")
        genuine_create = core.create_case

        def slow_create(*args, **kwargs):
            time.sleep(0.25)
            return genuine_create(*args, **kwargs)

        with patch.object(service.core, "create_case", side_effect=slow_create):
            with self.assertRaisesRegex(TimeoutError, r"setup .* /create:.*0\.1s"):
                client.call("/create", self.create_request())
            self.assertIsNone(client.conn)
            self.assertEqual(client.call("/health", {})["status"], "ok")

    def test_timeout_parameters_require_positive_finite_numbers(self):
        for field in ("timeout", "setup_timeout"):
            for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, "30", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "positive finite"):
                        self.client(**{field: value})

    def test_runner_closes_control_connection_before_workers_and_fault_probe(self):
        created_clients, events = [], []
        genuine_client = service.Client
        outer = self

        class TrackingClient(genuine_client):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created_clients.append(self)
                outer.clients.append(self)

            def call(self, endpoint, obj):
                events.append((endpoint, obj.get("fault"), self.conn is None))
                return super().call(endpoint, obj)

        def fake_worker(out, config):
            self.assertEqual(len(created_clients), 1)
            self.assertIsNone(created_clients[0].conn,
                              "Control connection must close before slow worker measurements")
            self.assertTrue(any(endpoint == "/query" for endpoint, _, _ in events))
            events.append(("worker", config["source"], True))
            return {"metrics": {"queries": 1, "accepted": 1}, "provider_samples": []}

        tiny_profile = {"sizes": [1], "queries": 1, "warmup": 0, "trials": 1,
                        "pool": 1, "memory_limit_mib": 128, "pressure_mib": 1,
                        "idle_seconds": 0}
        with patch.dict(runner.PROFILES, {"smoke": tiny_profile}), \
                patch.object(core, "MODES", ("leaf",)), \
                patch.object(runner, "Client", TrackingClient), \
                patch.object(runner, "_child", side_effect=fake_worker), \
                redirect_stdout(io.StringIO()):
            out = runner.run_suite({"out": self.root / "run", "role": "hpc", "profile": "smoke",
                                    "skip_original": True, "skip_migration": True,
                                    "provider": self.url, "token_file": self.token_file,
                                    "setup_timeout": 17})
        self.assertEqual(len([event for event in events if event[0] == "worker"]), 3)
        self.assertEqual([event for event in events if event[1] == "disconnect"],
                         [("/query", "disconnect", True)])
        manifest = json.loads((out / "run_manifest.json").read_text())
        self.assertEqual(manifest["config"]["setup_timeout_seconds"], 17)
        self.assertEqual(manifest["failed_records"], 0)
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        disconnect = next(row for row in rows if row["experiment"] == "disconnect")
        self.assertEqual(disconnect["status"], "ok")
        self.assertTrue(disconnect["metrics"]["recovered"])


if __name__ == "__main__":
    unittest.main()
