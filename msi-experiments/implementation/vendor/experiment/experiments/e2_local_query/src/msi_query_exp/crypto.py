from __future__ import annotations
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import mmap
import os
import platform
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from .constants import ED25519_PUBLIC_BYTES, ED25519_SIGNATURE_BYTES

@dataclass
class OperationCounter:
    hash_calls: int = 0
    signature_verifications: int = 0
    allocated_bytes: int = 0
    allocations: int = 0
    by_phase_hash_calls: dict[str, int] = field(default_factory=dict)
    by_phase_allocated_bytes: dict[str, int] = field(default_factory=dict)
    phase: str = 'unclassified'

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def record_hash(self, obj: bytes) -> None:
        self.hash_calls += 1
        self.by_phase_hash_calls[self.phase] = self.by_phase_hash_calls.get(self.phase, 0) + 1
        self.record_allocation(obj)

    def record_allocation(self, obj: object, logical_size: int | None=None) -> None:
        try:
            size = sys.getsizeof(obj) if logical_size is None else int(logical_size)
        except (TypeError, OverflowError):
            size = 0
        self.allocated_bytes += size
        self.allocations += 1
        self.by_phase_allocated_bytes[self.phase] = self.by_phase_allocated_bytes.get(self.phase, 0) + size

def sha256(data: bytes, counter: OperationCounter | None=None) -> bytes:
    out = hashlib.sha256(data).digest()
    if counter is not None:
        counter.record_hash(out)
    return out

class OpenSSLError(RuntimeError):
    pass

class Ed25519OpenSSL:

    def __init__(self) -> None:
        library = ctypes.util.find_library('crypto')
        if not library:
            raise OpenSSLError('libcrypto was not found by ctypes.util.find_library')
        try:
            self.lib = ctypes.CDLL(library)
        except OSError as exc:
            raise OpenSSLError(f'failed to load {library}: {exc}') from exc
        self.library = library
        self._configure()
        self.nid = int(self.lib.OBJ_sn2nid(b'ED25519'))
        if self.nid <= 0:
            raise OpenSSLError('this libcrypto does not expose the ED25519 algorithm')

    def _configure(self) -> None:
        lib = self.lib
        lib.OBJ_sn2nid.argtypes = [ctypes.c_char_p]
        lib.OBJ_sn2nid.restype = ctypes.c_int
        lib.EVP_PKEY_new_raw_private_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        lib.EVP_PKEY_new_raw_private_key.restype = ctypes.c_void_p
        lib.EVP_PKEY_new_raw_public_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        lib.EVP_PKEY_new_raw_public_key.restype = ctypes.c_void_p
        lib.EVP_PKEY_get_raw_public_key.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        lib.EVP_PKEY_get_raw_public_key.restype = ctypes.c_int
        lib.EVP_PKEY_free.argtypes = [ctypes.c_void_p]
        lib.EVP_PKEY_free.restype = None
        lib.EVP_MD_CTX_new.argtypes = []
        lib.EVP_MD_CTX_new.restype = ctypes.c_void_p
        lib.EVP_MD_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_MD_CTX_free.restype = None
        lib.EVP_DigestSignInit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.EVP_DigestSignInit.restype = ctypes.c_int
        lib.EVP_DigestSign.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        lib.EVP_DigestSign.restype = ctypes.c_int
        lib.EVP_DigestVerifyInit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.EVP_DigestVerifyInit.restype = ctypes.c_int
        lib.EVP_DigestVerify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        lib.EVP_DigestVerify.restype = ctypes.c_int
        if hasattr(lib, 'OpenSSL_version'):
            lib.OpenSSL_version.argtypes = [ctypes.c_int]
            lib.OpenSSL_version.restype = ctypes.c_char_p

    def version(self) -> str | None:
        try:
            if hasattr(self.lib, 'OpenSSL_version'):
                raw = self.lib.OpenSSL_version(0)
                return raw.decode('utf-8', errors='replace') if raw else None
        except Exception:
            return None
        return None

    def _private_key(self, seed32: bytes) -> ctypes.c_void_p:
        if len(seed32) != 32:
            raise ValueError('Ed25519 private seed must be 32 bytes')
        buf = ctypes.create_string_buffer(seed32)
        key = self.lib.EVP_PKEY_new_raw_private_key(self.nid, None, buf, len(seed32))
        if not key:
            raise OpenSSLError('EVP_PKEY_new_raw_private_key failed')
        return ctypes.c_void_p(key)

    def _public_key(self, public32: bytes) -> ctypes.c_void_p:
        if len(public32) != ED25519_PUBLIC_BYTES:
            raise ValueError('Ed25519 public key must be 32 bytes')
        buf = ctypes.create_string_buffer(public32)
        key = self.lib.EVP_PKEY_new_raw_public_key(self.nid, None, buf, len(public32))
        if not key:
            raise OpenSSLError('EVP_PKEY_new_raw_public_key failed')
        return ctypes.c_void_p(key)

    def public_from_seed(self, seed32: bytes) -> bytes:
        key = self._private_key(seed32)
        try:
            out = ctypes.create_string_buffer(ED25519_PUBLIC_BYTES)
            out_len = ctypes.c_size_t(ED25519_PUBLIC_BYTES)
            if self.lib.EVP_PKEY_get_raw_public_key(key, out, ctypes.byref(out_len)) != 1:
                raise OpenSSLError('EVP_PKEY_get_raw_public_key failed')
            if out_len.value != ED25519_PUBLIC_BYTES:
                raise OpenSSLError(f'unexpected Ed25519 public-key length {out_len.value}')
            return out.raw[:out_len.value]
        finally:
            self.lib.EVP_PKEY_free(key)

    def sign(self, seed32: bytes, message: bytes) -> bytes:
        key = self._private_key(seed32)
        ctx = self.lib.EVP_MD_CTX_new()
        if not ctx:
            self.lib.EVP_PKEY_free(key)
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if self.lib.EVP_DigestSignInit(ctx, None, None, None, key) != 1:
                raise OpenSSLError('EVP_DigestSignInit failed')
            sig = ctypes.create_string_buffer(ED25519_SIGNATURE_BYTES)
            sig_len = ctypes.c_size_t(ED25519_SIGNATURE_BYTES)
            msg = ctypes.create_string_buffer(message)
            if self.lib.EVP_DigestSign(ctx, sig, ctypes.byref(sig_len), msg, len(message)) != 1:
                raise OpenSSLError('EVP_DigestSign failed')
            if sig_len.value != ED25519_SIGNATURE_BYTES:
                raise OpenSSLError(f'unexpected Ed25519 signature length {sig_len.value}')
            return sig.raw[:sig_len.value]
        finally:
            self.lib.EVP_MD_CTX_free(ctx)
            self.lib.EVP_PKEY_free(key)

    def verify(self, public32: bytes, message: bytes, signature64: bytes, counter: OperationCounter | None=None) -> bool:
        if len(signature64) != ED25519_SIGNATURE_BYTES:
            return False
        key = self._public_key(public32)
        ctx = self.lib.EVP_MD_CTX_new()
        if not ctx:
            self.lib.EVP_PKEY_free(key)
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if self.lib.EVP_DigestVerifyInit(ctx, None, None, None, key) != 1:
                raise OpenSSLError('EVP_DigestVerifyInit failed')
            sig = ctypes.create_string_buffer(signature64)
            msg = ctypes.create_string_buffer(message)
            outcome = self.lib.EVP_DigestVerify(ctx, sig, len(signature64), msg, len(message))
            if counter is not None:
                counter.signature_verifications += 1
            return outcome == 1
        finally:
            self.lib.EVP_MD_CTX_free(ctx)
            self.lib.EVP_PKEY_free(key)

class CycleCounter:

    def __init__(self) -> None:
        self.fd: int | None = None
        self.source = 'unavailable'
        self.error: str | None = None
        self._start_fn: Callable[[], int] | None = None
        self._end_fn: Callable[[], int] | None = None
        self._mmap: mmap.mmap | None = None
        self._baseline = 0
        self._try_perf()
        if self.fd is None:
            self._try_tsc()

    def _try_perf(self) -> None:
        syscall_number = {'x86_64': 298, 'amd64': 298, 'aarch64': 241, 'arm64': 241}.get(platform.machine().lower())
        if syscall_number is None or platform.system() != 'Linux':
            self.error = 'perf_event_open syscall number unavailable for this platform'
            return
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        attr = bytearray(128)
        struct.pack_into('IIQ', attr, 0, 0, len(attr), 0)
        flags = 1 << 0 | 1 << 5 | 1 << 6
        struct.pack_into('Q', attr, 40, flags)
        attr_buffer = (ctypes.c_char * len(attr)).from_buffer(attr)
        fd = int(libc.syscall(syscall_number, ctypes.byref(attr_buffer), 0, -1, -1, 0))
        if fd < 0:
            err = ctypes.get_errno()
            self.error = f'perf_event_open failed: errno={err} ({os.strerror(err)})'
            return
        self.fd = fd
        self.source = 'perf_user_cpu_cycles'

    def _try_tsc(self) -> None:
        if platform.machine().lower() not in {'x86_64', 'amd64'}:
            return
        try:
            start_code = bytes.fromhex('0faee80f3148c1e2204809d0c3')
            end_code = bytes.fromhex('0f01f90faee848c1e2204809d0c3')
            mm = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            mm.write(start_code)
            end_offset = 64
            mm.seek(end_offset)
            mm.write(end_code)
            base = ctypes.addressof(ctypes.c_char.from_buffer(mm))
            function_type = ctypes.CFUNCTYPE(ctypes.c_uint64)
            self._start_fn = function_type(base)
            self._end_fn = function_type(base + end_offset)
            self._mmap = mm
            self.source = 'x86_tsc_ticks'
        except (OSError, ValueError) as exc:
            prior = f'; {self.error}' if self.error else ''
            self.error = f'TSC fallback failed: {exc}{prior}'

    def start(self) -> None:
        if self.fd is not None:
            fcntl.ioctl(self.fd, 9219, 0)
            fcntl.ioctl(self.fd, 9216, 0)
        elif self._start_fn is not None:
            self._baseline = int(self._start_fn())

    def stop(self) -> int | None:
        if self.fd is not None:
            fcntl.ioctl(self.fd, 9217, 0)
            raw = os.read(self.fd, 8)
            return int(struct.unpack('Q', raw)[0])
        if self._end_fn is not None:
            return int(self._end_fn()) - self._baseline
        return None

    def metadata(self) -> dict[str, Any]:
        return {'source': self.source, 'error': self.error}

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None

@dataclass(frozen=True)
class RaplDomain:
    name: str
    energy_path: Path
    max_range_uj: int | None

class RaplEnergyMeter:

    def __init__(self) -> None:
        self.domains: list[RaplDomain] = []
        self.error: str | None = None
        self._start: dict[str, int] = {}
        self._discover()

    def _discover(self) -> None:
        root = Path('/sys/class/powercap')
        if not root.exists():
            self.error = '/sys/class/powercap is unavailable'
            return
        candidates: list[RaplDomain] = []
        for energy_path in sorted(root.glob('**/energy_uj')):
            parent = energy_path.parent
            if parent.name.count(':') > 1:
                continue
            try:
                name = (parent / 'name').read_text(encoding='utf-8').strip()
                int(energy_path.read_text(encoding='utf-8').strip())
            except (OSError, ValueError):
                continue
            max_path = parent / 'max_energy_range_uj'
            try:
                max_range = int(max_path.read_text(encoding='utf-8').strip())
            except (OSError, ValueError):
                max_range = None
            candidates.append(RaplDomain(name=name, energy_path=energy_path, max_range_uj=max_range))
        if not candidates:
            self.error = 'no readable top-level powercap energy_uj domains'
            return
        self.domains = candidates

    def available(self) -> bool:
        return bool(self.domains)

    def start(self) -> None:
        self._start = {}
        for domain in self.domains:
            self._start[str(domain.energy_path)] = int(domain.energy_path.read_text(encoding='utf-8').strip())

    def stop(self) -> float | None:
        if not self.domains:
            return None
        total_uj = 0
        for domain in self.domains:
            key = str(domain.energy_path)
            before = self._start[key]
            after = int(domain.energy_path.read_text(encoding='utf-8').strip())
            if after >= before:
                delta = after - before
            elif domain.max_range_uj is not None:
                delta = domain.max_range_uj - before + after
            else:
                raise RuntimeError(f'RAPL counter wrapped without max range: {domain.name}')
            total_uj += delta
        return total_uj / 1000000.0

    def metadata(self) -> dict[str, Any]:
        return {'available': self.available(), 'error': self.error, 'domains': [{'name': d.name, 'energy_path': str(d.energy_path), 'max_range_uj': d.max_range_uj} for d in self.domains]}

class Ed25519Verifier:

    def __init__(self, backend: Ed25519OpenSSL, public32: bytes) -> None:
        self.backend = backend
        self.public_key = public32
        self._key = backend._public_key(public32)
        self._closed = False

    def verify(self, message: bytes, signature64: bytes, counter: OperationCounter | None=None) -> bool:
        if self._closed:
            raise RuntimeError('Ed25519 verifier is closed')
        if len(signature64) != ED25519_SIGNATURE_BYTES:
            return False
        lib = self.backend.lib
        ctx = lib.EVP_MD_CTX_new()
        if not ctx:
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if lib.EVP_DigestVerifyInit(ctx, None, None, None, self._key) != 1:
                raise OpenSSLError('EVP_DigestVerifyInit failed')
            sig = ctypes.create_string_buffer(signature64)
            msg = ctypes.create_string_buffer(message)
            outcome = lib.EVP_DigestVerify(ctx, sig, len(signature64), msg, len(message))
            if counter is not None:
                counter.signature_verifications += 1
            return outcome == 1
        finally:
            lib.EVP_MD_CTX_free(ctx)

    def close(self) -> None:
        if not self._closed:
            self.backend.lib.EVP_PKEY_free(self._key)
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
