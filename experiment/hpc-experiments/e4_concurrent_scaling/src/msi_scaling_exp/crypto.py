from __future__ import annotations
import ctypes, ctypes.util, hashlib, threading

class OpenSSLError(RuntimeError):
    pass

class _OpenSSL:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        name = ctypes.util.find_library('crypto')
        if not name:
            raise OpenSSLError('libcrypto was not found')
        self.library_name = name
        self.lib = ctypes.CDLL(name)
        L = self.lib
        L.OBJ_sn2nid.argtypes = [ctypes.c_char_p]
        L.OBJ_sn2nid.restype = ctypes.c_int
        self.nid = int(L.OBJ_sn2nid(b'ED25519'))
        if self.nid <= 0:
            raise OpenSSLError('Ed25519 unavailable')
        L.EVP_PKEY_new_raw_private_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.EVP_PKEY_new_raw_private_key.restype = ctypes.c_void_p
        L.EVP_PKEY_new_raw_public_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.EVP_PKEY_new_raw_public_key.restype = ctypes.c_void_p
        L.EVP_PKEY_get_raw_public_key.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        L.EVP_PKEY_get_raw_public_key.restype = ctypes.c_int
        L.EVP_PKEY_free.argtypes = [ctypes.c_void_p]
        L.EVP_MD_CTX_new.restype = ctypes.c_void_p
        L.EVP_MD_CTX_free.argtypes = [ctypes.c_void_p]
        L.EVP_DigestSignInit.argtypes = [ctypes.c_void_p] * 5
        L.EVP_DigestSignInit.restype = ctypes.c_int
        L.EVP_DigestSign.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        L.EVP_DigestSign.restype = ctypes.c_int
        L.EVP_DigestVerifyInit.argtypes = [ctypes.c_void_p] * 5
        L.EVP_DigestVerifyInit.restype = ctypes.c_int
        L.EVP_DigestVerify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        L.EVP_DigestVerify.restype = ctypes.c_int
        if hasattr(L, 'OpenSSL_version'):
            L.OpenSSL_version.argtypes = [ctypes.c_int]
            L.OpenSSL_version.restype = ctypes.c_char_p

    def version(self):
        if hasattr(self.lib, 'OpenSSL_version'):
            r = self.lib.OpenSSL_version(0)
            return r.decode(errors='replace') if r else None

class Ed25519Signer:

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError('seed length')
        self.o = _OpenSSL()
        buf = ctypes.create_string_buffer(seed)
        k = self.o.lib.EVP_PKEY_new_raw_private_key(self.o.nid, None, buf, 32)
        if not k:
            raise OpenSSLError('private key failed')
        self.key = ctypes.c_void_p(k)
        out = ctypes.create_string_buffer(32)
        n = ctypes.c_size_t(32)
        if self.o.lib.EVP_PKEY_get_raw_public_key(self.key, out, ctypes.byref(n)) != 1:
            raise OpenSSLError('public key failed')
        self.public_key = out.raw[:32]

    def sign(self, msg: bytes) -> bytes:
        c = self.o.lib.EVP_MD_CTX_new()
        try:
            if self.o.lib.EVP_DigestSignInit(c, None, None, None, self.key) != 1:
                raise OpenSSLError('sign init')
            sig = ctypes.create_string_buffer(64)
            n = ctypes.c_size_t(64)
            b = ctypes.create_string_buffer(msg)
            if self.o.lib.EVP_DigestSign(c, sig, ctypes.byref(n), b, len(msg)) != 1:
                raise OpenSSLError('sign')
            return sig.raw[:n.value]
        finally:
            self.o.lib.EVP_MD_CTX_free(c)

    def close(self):
        if getattr(self, 'key', None):
            self.o.lib.EVP_PKEY_free(self.key)
            self.key = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

class Ed25519Verifier:

    def __init__(self, pub: bytes):
        if len(pub) != 32:
            raise ValueError('pub length')
        self.o = _OpenSSL()
        b = ctypes.create_string_buffer(pub)
        k = self.o.lib.EVP_PKEY_new_raw_public_key(self.o.nid, None, b, 32)
        if not k:
            raise OpenSSLError('public key failed')
        self.key = ctypes.c_void_p(k)

    def verify(self, msg: bytes, sig: bytes) -> bool:
        if len(sig) != 64:
            return False
        c = self.o.lib.EVP_MD_CTX_new()
        try:
            if self.o.lib.EVP_DigestVerifyInit(c, None, None, None, self.key) != 1:
                raise OpenSSLError('verify init')
            sb = ctypes.create_string_buffer(sig)
            mb = ctypes.create_string_buffer(msg)
            return self.o.lib.EVP_DigestVerify(c, sb, 64, mb, len(msg)) == 1
        finally:
            self.o.lib.EVP_MD_CTX_free(c)

    def close(self):
        if getattr(self, 'key', None):
            self.o.lib.EVP_PKEY_free(self.key)
            self.key = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

def deterministic_seed(label: str, seed: int) -> bytes:
    return hashlib.sha256(b'MSI-E4-KEY\x00' + label.encode() + seed.to_bytes(8, 'big')).digest()

def openssl_info():
    try:
        o = _OpenSSL()
        s = Ed25519Signer(bytes(range(32)))
        v = Ed25519Verifier(s.public_key)
        msg = b'e4-selftest'
        sig = s.sign(msg)
        ok = v.verify(msg, sig)
        s.close()
        v.close()
        return {'available': ok, 'library': o.library_name, 'version': o.version()}
    except Exception as e:
        return {'available': False, 'error': f'{type(e).__name__}: {e}'}
