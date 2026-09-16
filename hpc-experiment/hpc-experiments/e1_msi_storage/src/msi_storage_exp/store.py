from __future__ import annotations
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from .constants import OBJECT_RECORD_OVERHEAD_BYTES, OBJECT_STORE_HEADER_BYTES
from .util import allocated_bytes

@dataclass
class FileMeasurement:
    path: str
    logical_bytes: int
    allocated_bytes: int
    records: int

class FixedRecordFile:

    def __init__(self, path: Path, magic: bytes):
        if len(magic) > 8:
            raise ValueError('file magic must be at most 8 bytes')
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open('wb', buffering=1024 * 1024)
        header = magic.ljust(8, b'\x00') + struct.pack('>II', 1, 0)
        self._fh.write(header)
        self.records = 0

    def append(self, record: bytes) -> None:
        self._fh.write(record)
        self.records += 1

    def close(self) -> FileMeasurement:
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        return FileMeasurement(path=str(self.path), logical_bytes=self.path.stat().st_size, allocated_bytes=allocated_bytes(self.path), records=self.records)

    def __enter__(self) -> 'FixedRecordFile':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._fh.closed:
            self._fh.close()

class ObjectStoreFile:

    def __init__(self, path: Path, magic: bytes):
        if len(magic) > 8:
            raise ValueError('file magic must be at most 8 bytes')
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open('wb', buffering=1024 * 1024)
        self._fh.write(magic.ljust(8, b'\x00') + struct.pack('>II', 1, 0))
        self.records = 0
        self.value_bytes = 0

    def append(self, key: bytes, value: bytes) -> None:
        if len(key) != 32:
            raise ValueError('object keys must be 32 bytes')
        prefix = key + struct.pack('>Q', len(value))
        crc = struct.pack('>I', zlib.crc32(prefix + value) & 4294967295)
        self._fh.write(prefix)
        self._fh.write(value)
        self._fh.write(crc)
        self.records += 1
        self.value_bytes += len(value)

    def close(self) -> tuple[FileMeasurement, int]:
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        measurement = FileMeasurement(path=str(self.path), logical_bytes=self.path.stat().st_size, allocated_bytes=allocated_bytes(self.path), records=self.records)
        expected = OBJECT_STORE_HEADER_BYTES + self.records * OBJECT_RECORD_OVERHEAD_BYTES + self.value_bytes
        if measurement.logical_bytes != expected:
            raise AssertionError(f'object-store length {measurement.logical_bytes} != expected {expected}')
        return (measurement, self.value_bytes)

    def __enter__(self) -> 'ObjectStoreFile':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._fh.closed:
            self._fh.close()
