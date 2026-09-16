




from __future__ import annotations
import base64
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from . import core, metrics

MAX_BODY = 96 * 1024 * 1024
MODES = ('archive', 'full', 'leaf', 'ext_cached', 'ext_rebuild')

def read_token(path: Path) -> str:
    token = path.read_text().strip()
    if len(token) < 24 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise ValueError('Token must contain at least 24 URL-safe characters')
    return token

def create_token(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secrets.token_urlsafe(32) + '\n')

class ExperimentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_root: Path, token: str):
        self.data_root = data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.case_lock = threading.Lock()
        self.platform = metrics.system_info()
        self.platform['provider_storage'] = metrics.storage_for_path(self.data_root)
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'MSI-Supplement/1'

    def log_message(self, *_):
        pass  

    def send_json(self, value, status=200):
        body = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.close_connection = True
            self.send_json({'error': 'unauthorised'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= MAX_BODY:
                raise ValueError('request body outside allowed size')
            self.connection.settimeout(60)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete request')
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError('JSON object required')
            if self.path == '/health':
                self.send_json({'status': 'ok', 'platform': self.server.platform,
                                'wire_format': 'http1-json-base64', 'artificial_delay': False})
            elif self.path == '/create':
                mode = obj.get('mode')
                if mode not in MODES:
                    raise ValueError('unsupported mode')
                encoded = obj.get('payloads_b64')
                if not isinstance(encoded, list) or not 1 <= len(encoded) <= 65536:
                    raise ValueError('invalid payload count')
                payloads = [base64.b64decode(x, validate=True) for x in encoded]
                if sum(map(len, payloads)) > 64 * 1024 * 1024:
                    raise ValueError('one experiment case is limited to 64 MiB payload')
                case_id = secrets.token_hex(12)
                path = self.server.data_root / case_id
                with self.server.case_lock:
                    descriptor = core.create_case(payloads, path, mode)
                self.send_json({'case_id': case_id, 'descriptor': descriptor,
                                'accounting': core.storage_accounting(path),
                                'platform': self.server.platform})
            elif self.path == '/query':
                case_id = obj.get('case_id', '')
                if not isinstance(case_id, str) or not re.fullmatch(r'[0-9a-f]{24}', case_id):
                    raise ValueError('invalid case id')
                path = self.server.data_root / case_id
                if not path.is_dir():
                    raise ValueError('unknown case')
                if obj.get('fault') == 'disconnect':
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                start = time.perf_counter_ns()
                response = core.query_store(path, obj['position'])
                response['provider']['handler_query_ns'] = time.perf_counter_ns() - start
                response['provider']['rss_bytes'] = metrics.rss_bytes()
                self.send_json(response)
            else:
                self.send_json({'error': 'unknown endpoint'}, 404)
        except (ValueError, KeyError, TypeError, OSError, MemoryError) as exc:
            self.close_connection = True
            try:
                self.send_json({'error': type(exc).__name__ + ': ' + str(exc)}, 400)
            except (OSError, BrokenPipeError):
                pass

class Client:
    def __init__(self, url: str, token: str, timeout: float = 30):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('provider URL must be http(s)://host:port without credentials')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('provider URL must not contain a path, query, or fragment')
        self.parsed, self.token, self.timeout = parsed, token, timeout
        self.conn = None
        self.last_request_bytes = self.last_response_bytes = 0

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = None

    def call(self, endpoint: str, obj: dict):
        if self.conn is None:
            cls = http.client.HTTPSConnection if self.parsed.scheme == 'https' else http.client.HTTPConnection
            self.conn = cls(self.parsed.hostname, self.parsed.port, timeout=self.timeout)
        body = json.dumps(obj, separators=(',', ':'), allow_nan=False).encode()
        if len(body) > MAX_BODY:
            raise ValueError('request exceeds size bound')
        self.last_request_bytes = len(body)
        try:
            self.conn.request('POST', endpoint, body, {'Content-Type': 'application/json',
                                                     'Authorization': 'Bearer ' + self.token})
            reply = self.conn.getresponse()
            raw = reply.read(MAX_BODY + 1)
            self.last_response_bytes = len(raw)
            if len(raw) > MAX_BODY:
                raise ValueError('response exceeds size bound')
            parsed = json.loads(raw)
            if reply.status != 200:
                raise RuntimeError(f'Provider returned HTTP {reply.status}: {parsed.get("error", "error")}')
            return parsed
        except Exception:
            self.close()
            raise
