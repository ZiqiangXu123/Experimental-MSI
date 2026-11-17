from __future__ import annotations
import ctypes
import ctypes.util
import hashlib
from dataclasses import dataclass, field
from typing import Any
from .constants import ED25519_PUBLIC_BYTES, ED25519_SIGNATURE_BYTES

@dataclass
class OperationCounter:
    hash_calls: int = 0
    signature_verifications: int = 0
    by_phase_hash_calls: dict[str, int] = field(default_factory=dict)
    phase: str = 'unclassified'

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def record_hash(self) -> None:
        self.hash_calls += 1
        self.by_phase_hash_calls[self.phase] = self.by_phase_hash_calls.get(self.phase, 0) + 1

def sha256(data: bytes, counter: OperationCounter | None=None) -> bytes:
    out = hashlib.sha256(data).digest()
    if counter is not None:
        counter.record_hash()
    return out

class OpenSSLError(RuntimeError):
    pass

class Ed25519OpenSSL:

    def __init__(self) -> None:
        library = ctypes.util.find_library('crypto')
        if not library:
            raise OpenSSLError('libcrypto was not found')
        try:
            self.lib = ctypes.CDLL(library)
        except OSError as exc:
            raise OpenSSLError(f'failed to load {library}: {exc}') from exc
        self.library = library
        self._configure()
        self.nid = int(self.lib.OBJ_sn2nid(b'ED25519'))
        if self.nid <= 0:
            raise OpenSSLError('libcrypto does not expose ED25519')

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
        if not hasattr(self.lib, 'OpenSSL_version'):
            return None
        raw = self.lib.OpenSSL_version(0)
        return raw.decode('utf-8', errors='replace') if raw else None

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
                raise OpenSSLError('unexpected public-key width')
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
                raise OpenSSLError('unexpected signature width')
            return sig.raw[:sig_len.value]
        finally:
            self.lib.EVP_MD_CTX_free(ctx)
            self.lib.EVP_PKEY_free(key)

    def verify(self, public32: bytes, message: bytes, signature64: bytes, counter: OperationCounter | None=None) -> bool:
        if len(signature64) != ED25519_SIGNATURE_BYTES:
            return False
        try:
            key = self._public_key(public32)
        except (OpenSSLError, ValueError):
            return False
        ctx = self.lib.EVP_MD_CTX_new()
        if not ctx:
            self.lib.EVP_PKEY_free(key)
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if self.lib.EVP_DigestVerifyInit(ctx, None, None, None, key) != 1:
                raise OpenSSLError('EVP_DigestVerifyInit failed')
            sig = ctypes.create_string_buffer(signature64)
            msg = ctypes.create_string_buffer(message)
            result = self.lib.EVP_DigestVerify(ctx, sig, len(signature64), msg, len(message))
            if counter is not None:
                counter.signature_verifications += 1
            return result == 1
        finally:
            self.lib.EVP_MD_CTX_free(ctx)
            self.lib.EVP_PKEY_free(key)

    def metadata(self) -> dict[str, Any]:
        return {'library': self.library, 'version': self.version(), 'available': True}
