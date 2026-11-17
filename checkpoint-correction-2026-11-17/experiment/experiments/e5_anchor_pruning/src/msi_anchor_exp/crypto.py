from __future__ import annotations
import ctypes
import ctypes.util
import hashlib
import threading
from typing import Any

class OpenSSLError(RuntimeError):
    pass

class _OpenSSL:
    _instance: '_OpenSSL | None' = None
    _lock = threading.Lock()

    def __new__(cls) -> '_OpenSSL':
        with cls._lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialise()
                cls._instance = instance
            return cls._instance

    def _initialise(self) -> None:
        library_name = ctypes.util.find_library('crypto')
        if not library_name:
            raise OpenSSLError('libcrypto was not found')
        self.library_name = library_name
        self.lib = ctypes.CDLL(library_name)
        library = self.lib
        library.OBJ_sn2nid.argtypes = [ctypes.c_char_p]
        library.OBJ_sn2nid.restype = ctypes.c_int
        self.nid = int(library.OBJ_sn2nid(b'ED25519'))
        if self.nid <= 0:
            raise OpenSSLError('Ed25519 is unavailable in libcrypto')
        library.EVP_PKEY_new_raw_private_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        library.EVP_PKEY_new_raw_private_key.restype = ctypes.c_void_p
        library.EVP_PKEY_new_raw_public_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        library.EVP_PKEY_new_raw_public_key.restype = ctypes.c_void_p
        library.EVP_PKEY_get_raw_public_key.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        library.EVP_PKEY_get_raw_public_key.restype = ctypes.c_int
        library.EVP_PKEY_free.argtypes = [ctypes.c_void_p]
        library.EVP_MD_CTX_new.restype = ctypes.c_void_p
        library.EVP_MD_CTX_free.argtypes = [ctypes.c_void_p]
        library.EVP_DigestSignInit.argtypes = [ctypes.c_void_p] * 5
        library.EVP_DigestSignInit.restype = ctypes.c_int
        library.EVP_DigestSign.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        library.EVP_DigestSign.restype = ctypes.c_int
        library.EVP_DigestVerifyInit.argtypes = [ctypes.c_void_p] * 5
        library.EVP_DigestVerifyInit.restype = ctypes.c_int
        library.EVP_DigestVerify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        library.EVP_DigestVerify.restype = ctypes.c_int
        if hasattr(library, 'OpenSSL_version'):
            library.OpenSSL_version.argtypes = [ctypes.c_int]
            library.OpenSSL_version.restype = ctypes.c_char_p

    def version(self) -> str | None:
        if not hasattr(self.lib, 'OpenSSL_version'):
            return None
        value = self.lib.OpenSSL_version(0)
        return value.decode(errors='replace') if value else None

class Ed25519Signer:

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError('Ed25519 seed must contain exactly 32 bytes')
        self.openssl = _OpenSSL()
        seed_buffer = ctypes.create_string_buffer(seed)
        key = self.openssl.lib.EVP_PKEY_new_raw_private_key(self.openssl.nid, None, seed_buffer, 32)
        if not key:
            raise OpenSSLError('EVP_PKEY_new_raw_private_key failed')
        self.key = ctypes.c_void_p(key)
        public_buffer = ctypes.create_string_buffer(32)
        public_length = ctypes.c_size_t(32)
        if self.openssl.lib.EVP_PKEY_get_raw_public_key(self.key, public_buffer, ctypes.byref(public_length)) != 1:
            self.close()
            raise OpenSSLError('EVP_PKEY_get_raw_public_key failed')
        self.public_key = public_buffer.raw[:public_length.value]

    def sign(self, message: bytes) -> bytes:
        context = self.openssl.lib.EVP_MD_CTX_new()
        if not context:
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if self.openssl.lib.EVP_DigestSignInit(context, None, None, None, self.key) != 1:
                raise OpenSSLError('EVP_DigestSignInit failed')
            signature_buffer = ctypes.create_string_buffer(64)
            signature_length = ctypes.c_size_t(64)
            message_buffer = ctypes.create_string_buffer(message)
            if self.openssl.lib.EVP_DigestSign(context, signature_buffer, ctypes.byref(signature_length), message_buffer, len(message)) != 1:
                raise OpenSSLError('EVP_DigestSign failed')
            signature = signature_buffer.raw[:signature_length.value]
            if len(signature) != 64:
                raise OpenSSLError(f'unexpected Ed25519 signature length: {len(signature)}')
            return signature
        finally:
            self.openssl.lib.EVP_MD_CTX_free(context)

    def close(self) -> None:
        if getattr(self, 'key', None):
            self.openssl.lib.EVP_PKEY_free(self.key)
            self.key = None

    def __enter__(self) -> 'Ed25519Signer':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

class Ed25519Verifier:

    def __init__(self, public_key: bytes):
        if len(public_key) != 32:
            raise ValueError('Ed25519 public key must contain exactly 32 bytes')
        self.openssl = _OpenSSL()
        public_buffer = ctypes.create_string_buffer(public_key)
        key = self.openssl.lib.EVP_PKEY_new_raw_public_key(self.openssl.nid, None, public_buffer, 32)
        if not key:
            raise OpenSSLError('EVP_PKEY_new_raw_public_key failed')
        self.key = ctypes.c_void_p(key)

    def verify(self, message: bytes, signature: bytes) -> bool:
        if len(signature) != 64:
            return False
        context = self.openssl.lib.EVP_MD_CTX_new()
        if not context:
            raise OpenSSLError('EVP_MD_CTX_new failed')
        try:
            if self.openssl.lib.EVP_DigestVerifyInit(context, None, None, None, self.key) != 1:
                raise OpenSSLError('EVP_DigestVerifyInit failed')
            signature_buffer = ctypes.create_string_buffer(signature)
            message_buffer = ctypes.create_string_buffer(message)
            return self.openssl.lib.EVP_DigestVerify(context, signature_buffer, len(signature), message_buffer, len(message)) == 1
        finally:
            self.openssl.lib.EVP_MD_CTX_free(context)

    def close(self) -> None:
        if getattr(self, 'key', None):
            self.openssl.lib.EVP_PKEY_free(self.key)
            self.key = None

    def __enter__(self) -> 'Ed25519Verifier':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

def deterministic_seed(label: str, seed: int) -> bytes:
    return hashlib.sha256(b'MSI-E5-KEY\x00' + label.encode('utf-8') + int(seed).to_bytes(8, 'big')).digest()

def openssl_info() -> dict[str, Any]:
    try:
        openssl = _OpenSSL()
        signer = Ed25519Signer(bytes(range(32)))
        verifier = Ed25519Verifier(signer.public_key)
        message = b'e5-ed25519-self-test'
        signature = signer.sign(message)
        success = verifier.verify(message, signature)
        signer.close()
        verifier.close()
        return {'available': bool(success), 'library': openssl.library_name, 'version': openssl.version()}
    except Exception as exc:
        return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
