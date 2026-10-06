'''
Block device + ctypes bindings. Custom built for iso handling including stateless thread-safe read-only access.

Uses the NativeRegistry to load the prebuilt library or compile it on demand.
'''
from __future__ import annotations

import sys
import ctypes
import threading
import weakref
from pathlib import Path
from core.native.native_registry import NativeRegistry, NativeLibrary

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_lib: ctypes.CDLL | None = None
_load_attempted = False
_lock = threading.Lock()

###--------------------------------- C-Types Bindings ---------------------------------###

_LIBRARY_DEF = NativeLibrary(
    name='block_device',
    root_dir=Path(__file__).resolve().parent,
    sources=['block_device.c'],
)

def _bindings(lib: ctypes.CDLL) -> ctypes.CDLL:
    # alloc_aligned_buffer(size_t size, uint32_t alignment) -> void*
    lib.alloc_aligned_buffer.argtypes = [ctypes.c_size_t, ctypes.c_uint32]
    lib.alloc_aligned_buffer.restype = ctypes.c_void_p

    # free_aligned_buffer(void* ptr) -> void
    lib.free_aligned_buffer.argtypes = [ctypes.c_void_p]
    lib.free_aligned_buffer.restype = None

    # open_device(const char* path, uint32_t sector_size) -> void*
    lib.open_device.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
    lib.open_device.restype = ctypes.c_void_p

    # get_device_size(NativeBlockDevice* device) -> int64_t
    lib.get_device_size.argtypes = [ctypes.c_void_p]
    lib.get_device_size.restype = ctypes.c_uint64

    # read_sectors(NativeBlockDevice* device, int64_t byte_offset, void* out_buffer, size_t byte_count) -> int64_t
    lib.read_sectors.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_size_t]
    lib.read_sectors.restype = ctypes.c_int64

    # read_slice(NativeBlockDevice* device, uint64_t offset, size_t size, void* aligned_buffer, size_t buffer_capacity) -> int64_t
    lib.read_slice.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    lib.read_slice.restype = ctypes.c_int64

    # close_device(NativeBlockDevice* device) -> void
    lib.close_device.argtypes = [ctypes.c_void_p]
    lib.close_device.restype = None

    return lib

def get_block_device_lib() -> ctypes.CDLL:
    global _lib, _load_attempted
    if _load_attempted:
        if _lib is None:
            raise RuntimeError('Native BlockDevice library is unavailable.')
        return _lib
    with _lock:
        if _load_attempted:
            if _lib is None:
                raise RuntimeError('Native BlockDevice library is unavailable.')
            return _lib
        _load_attempted = True
        _lib = NativeRegistry.load(_LIBRARY_DEF, _bindings)
        if _lib is None:
            raise RuntimeError('Failed to load or build native BlockDevice library.')
        return _lib


###------------------------------------- BlockDevice -----------------------------------###

class ThreadLocalAlignedBuffer:
    '''
    RAII wrapper around an aligned buffer allocated for each thread.
    Destructor frees the buffer when the weakref is finalized better than __del__
    since it even catches after interpreter shutdown.

    Current scratch preallocated: 2MB + 64KB
    '''
    def __init__(self, lib, capacity: int = (1024 * 1024 * 64) + (1024 * 64), alignment: int = 2048) -> None:
        self._lib = lib
        self.capacity = capacity
        self.alignment = alignment
        self.ptr = self._lib.alloc_aligned_buffer(capacity, alignment)
        if not self.ptr:
            raise MemoryError(f'Failed to allocate {capacity} aligned bytes')
        weakref.finalize(self, self._lib.free_aligned_buffer, self.ptr)

class BlockDevice:
    '''
    Represents a native block device, providing read operations via a thread-local allocation.
    If the pre-allocated scratchpad size is exceeded, a fallback to a dynamic buffer is used.
    '''
    def __init__(self, path: str | Path, sector_size: int = 2048) -> None:
        self.path        = Path(path)
        self.sector_size = sector_size
        self.alignment   = max(sector_size, 2048)
        self._lib        = get_block_device_lib()
        path_bytes       = str(self.path).encode(sys.getfilesystemencoding() or 'utf-8')
        self._dev_handle = self._lib.open_device(path_bytes, sector_size)
        if not self._dev_handle:
            raise FileNotFoundError(f'Failed to open native block device: {self.path}')
        self._size  = self._lib.get_device_size(self._dev_handle)
        self.closed = False
        self._tls = threading.local()

        # Not completely thread-safe, but good enough for reporting read stats
        self.scratch_reads = 0
        self.large_reads = 0

    @property
    def _scratch_buffer(self) -> ThreadLocalAlignedBuffer:
        '''Fetch or create a persistent C-allocated buffer for the current thread'''
        if not hasattr(self._tls, 'buf'):
            self._tls.buf = ThreadLocalAlignedBuffer(self._lib, alignment=self.alignment)
        return self._tls.buf

    def _requires_large_read(self, offset: int, size: int, capacity: int) -> bool:
        '''Calculate if the read requires a large buffer (beyond the thread-local scratch buffer).'''
        return (size + (offset & (self.sector_size - 1)) + self.sector_size - 1) & ~(self.sector_size - 1) > capacity

    def pread(self, offset: int, size: int) -> bytes:
        '''Stateless, thread-safe, and zero-allocation read.'''
        if self.closed or not self._dev_handle:
            raise ValueError('I/O operation on closed BlockDevice')
        size = min(size, self._size - offset)
        if size <= 0:
            return b''
        # Fetch the thread-local scratch buffer
        tls_buf = self._scratch_buffer
        if self._requires_large_read(offset, size, tls_buf.capacity): # dynamic buffer fallback
            return self._pread_large(offset, size)
        skip_val = ctypes.c_size_t(0)
        # Get the slice
        available = self._lib.read_slice(
            self._dev_handle,
            offset,
            size,
            tls_buf.ptr,
            tls_buf.capacity,
            ctypes.byref(skip_val)
        )
        if available < 0:
            raise OSError(f'Failed to read {size} bytes at offset {offset}')
        skip = skip_val.value
        self.scratch_reads += 1
        return ctypes.string_at(tls_buf.ptr + skip, available) # Moving the ptr forward itself saves a copy

    def _pread_large(self, offset: int, size: int) -> bytes:
        '''
        Fallback for reads that exceed the TLS scratchpad capacity.
        Wastes resources recalculating alignments if triggered often increase scratchpad capacity.
        '''
        skip_bytes = offset & (self.sector_size - 1)
        aligned_offset = offset - skip_bytes
        aligned_size = (size + skip_bytes + self.sector_size - 1) & ~(self.sector_size - 1)
        temp_buf = self._lib.alloc_aligned_buffer(aligned_size, self.sector_size)
        if not temp_buf:
            raise MemoryError('Failed to allocate large aligned buffer.')
        try:
            bytes_read = self._lib.read_sectors(
                self._dev_handle,
                aligned_offset,
                temp_buf,
                aligned_size
            )
            if bytes_read < 0:
                raise OSError('Large read failed.')
            raw_data = ctypes.string_at(temp_buf, bytes_read)
            self.large_reads += 1
            available = max(0, bytes_read - skip_bytes)
            available = min(available, size)
            return raw_data[skip_bytes : skip_bytes + available]
        finally:
            self._lib.free_aligned_buffer(temp_buf)

    def pread_view(self, offset: int, size: int) -> memoryview:
        '''
        Zero-copy memoryview read using the scratch buffer.
        !!! Buffer is overwritten on subsequent read (on the same thread). !!!
        Can't read more than the scratch capacity.
        '''
        if self.closed or not self._dev_handle:
            raise ValueError('I/O operation on closed BlockDevice.')
        size = min(size, self._size - offset)
        if size <= 0:
            return memoryview(b'')
        tls_buf = self._scratch_buffer
        if self._requires_large_read(offset, size, tls_buf.capacity):
            raise BufferError(
                f'Requested read size {size} exceeds scratch buffer capacity {tls_buf.capacity}. '
                'Use pread()[pread_large] or read_into() instead.'
            )
        skip_val = ctypes.c_size_t(0)
        available = self._lib.read_slice(
            self._dev_handle, offset, size, tls_buf.ptr, tls_buf.capacity, ctypes.byref(skip_val)
        )
        if available < 0:
            raise OSError(f'Failed to read {size} bytes at offset {offset}')
        self.scratch_reads += 1
        c_array = (ctypes.c_char * available).from_address(tls_buf.ptr + skip_val.value)
        return memoryview(c_array)


    def readinto(self, buffer: memoryview | bytearray, offset: int) -> int:
        '''Zero-copy read into a pre-allocated python memoryview buffer.'''
        if self.closed or not self._dev_handle:
            raise ValueError('I/O operation on closed BlockDevice')
        size = min(len(buffer), getattr(self, '_size', 0) - offset)
        if size <= 0:
            return 0
        tls_buf = self._scratch_buffer
        # Fast path, read into scratchpad
        if not self._requires_large_read(offset, size, tls_buf.capacity):
            self.scratch_reads += 1
            buffer[:size] = self.pread_view(offset, size)
            return size
        # Slow path, read into temp aligned C-buffer
        skip_bytes = offset & (self.sector_size - 1)
        aligned_offset = offset - skip_bytes
        aligned_size = (size + skip_bytes + self.sector_size - 1) & ~(self.sector_size - 1)
        temp_buf = self._lib.alloc_aligned_buffer(aligned_size, self.sector_size)
        if not temp_buf:
            raise MemoryError('Failed to allocate large aligned buffer')
        try:
            bytes_read = self._lib.read_sectors(self._dev_handle, aligned_offset, temp_buf, aligned_size)
            if bytes_read < 0:
                raise OSError(f'Large read failed: {bytes_read}')
            available = max(0, bytes_read - skip_bytes)
            available = min(available, size)
            self.large_reads += 1
            c_array = (ctypes.c_char * available).from_address(temp_buf + skip_bytes)
            buffer[:available] = c_array
            return available
        finally:
            self._lib.free_aligned_buffer(temp_buf)

    @property
    def size(self) -> int:
        return self._size

    def reopen(self, sector_size: int) -> None:
        '''
        Reopens the device with the appropriate sector size post detection.
        Invalidates thread cached scratch buffer to ensure thread safety.
        '''
        if self.closed or not self._dev_handle:
            raise ValueError('Cannot reopen a closed BlockDevice')
        if sector_size == self.sector_size:
            return
        self._lib.close_device(self._dev_handle)
        path_bytes = str(self.path).encode(sys.getfilesystemencoding() or 'utf-8')
        self._dev_handle = self._lib.open_device(path_bytes, sector_size)
        if not self._dev_handle:
            raise ValueError(f'Failed to reopen BlockDevice, {self.path} with sector_size={sector_size}')
        self.sector_size = sector_size
        self.alignment   = max(sector_size, 2048)
        self._size       = self._lib.get_device_size(self._dev_handle)
        self._tls        = threading.local()
        print(f'Reopened {self.path.name} with sector_size={sector_size} (alignment={self.alignment})')


    def close(self) -> None:
        if not self.closed and getattr(self, '_dev_handle', None):
            self._lib.close_device(self._dev_handle)
            self._dev_handle = None
            self.closed = True
        logger.debug(
            f'Closed: {self.closed}, _dev_handle: {self._dev_handle}.\n'
            f'    Scratch reads: {self.scratch_reads}, Large reads: {self.large_reads}'
        )

    @property
    def name(self) -> str:
        '''Same functionality as Path.name'''
        return str(self.path.name)

    def __len__(self) -> int:
        return self._size

    def __str__(self) -> str:
        return str(self.path.name)

    def __repr__(self) -> str:
        return f'<BlockDevice {self.path}>'
