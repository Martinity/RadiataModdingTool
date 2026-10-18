'''
Block device + ctypes bindings. Custom built for iso handling including stateless thread-safe read-only access.

Uses the NativeRegistry to load the prebuilt library or compile it on demand.
'''
from __future__ import annotations

import errno, os, re, sys, ctypes, threading, weakref
from pathlib import Path
from core.native.native_registry import NativeRegistry, NativeLibrary

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_lib: ctypes.CDLL | None = None
_load_attempted = False
_lock = threading.Lock()

_IS_WINDOWS = sys.platform == 'win32'
_ERROR_OPERATION_ABORTED = 995
_ABORTED_CODE = _ERROR_OPERATION_ABORTED if _IS_WINDOWS else errno.ECANCELED

###--------------------------------- C-Types Bindings ---------------------------------###

_LIBRARY_DEF = NativeLibrary(
    name='block_device',
    root_dir=Path(__file__).resolve().parent,
    sources=['block_device.c'],
)

def _bindings(lib: ctypes.CDLL) -> ctypes.CDLL:
    try:
        # alloc_aligned_buffer(size_t size, uint32_t alignment) -> void*
        lib.alloc_aligned_buffer.argtypes = [ctypes.c_size_t, ctypes.c_uint32]
        lib.alloc_aligned_buffer.restype = ctypes.c_void_p

        # free_aligned_buffer(void* ptr) -> void
        lib.free_aligned_buffer.argtypes = [ctypes.c_void_p]
        lib.free_aligned_buffer.restype = None

        # open_device(const char* path, uint32_t sector_size, int32_t path_encoding) -> void*
        lib.open_device.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int32]
        lib.open_device.restype = ctypes.c_void_p

        # get_last_error(void) -> int32_t
        lib.get_last_error.argtypes = []
        lib.get_last_error.restype = ctypes.c_int32

        # get_device_size(NativeBlockDevice* device) -> int64_t
        lib.get_device_size.argtypes = [ctypes.c_void_p]
        lib.get_device_size.restype = ctypes.c_int64

        # get_sector_size/get_device_sector_size(NativeBlockDevice* device) -> uint32_t
        lib.get_sector_size.argtypes = [ctypes.c_void_p]
        lib.get_sector_size.restype = ctypes.c_uint32
        lib.get_device_sector_size.argtypes = [ctypes.c_void_p]
        lib.get_device_sector_size.restype = ctypes.c_uint32

        # is_raw_device(NativeBlockDevice* device) -> int32_t
        lib.is_raw_device.argtypes = [ctypes.c_void_p]
        lib.is_raw_device.restype = ctypes.c_int32

        # read_sectors(NativeBlockDevice* device, uint64_t byte_offset, void* out_buffer, size_t byte_count) -> int64_t
        lib.read_sectors.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
        lib.read_sectors.restype = ctypes.c_int64

        # read_slice(NativeBlockDevice* device, uint64_t offset, size_t size, void* aligned_buffer, size_t buffer_capacity, size_t* skip) -> int64_t
        lib.read_slice.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        lib.read_slice.restype = ctypes.c_int64

        # close_device(NativeBlockDevice* device, uint32_t timeout_ms) -> int32_t
        lib.close_device.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.close_device.restype = ctypes.c_int32

        # free_device(NativeBlockDevice* device) -> void
        lib.free_device.argtypes = [ctypes.c_void_p]
        lib.free_device.restype = None

    except AttributeError as e:
        raise RuntimeError(
            f'The native block_device library is out of date ({e}). '
            'Delete the cached/prebuilt block_device library so it can be rebuilt.'
        ) from e

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

###-------------------------------------- Helpers ----------------------------------------###

_WIN_BARE_DRIVE = re.compile(r'^(\\\\[.?]\\[A-Za-z]:)\\+$')

def _normalise_path(path: str) -> str:
    '''Drop trailing slashes from a bare Windows drive device so it names the raw volume'''
    if _IS_WINDOWS:
        bare_drive = _WIN_BARE_DRIVE.match(path)
    if bare_drive:
        return bare_drive.group(1)
    return path

def _os_error(code: int, path: str, action: str = 'open') -> OSError:
    '''Build the OSError the OS would have raised: PermissionError, FileNotFoundError... etc'''
    code = int(code)
    if code == 0:
        return OSError(f'Failed to {action} {path} (no OS error code was reported)')
    if _IS_WINDOWS:
        err = ctypes.WinError(code)
        err.filename = path
        return err
    return OSError(code, os.strerror(code), path)

###------------------------------------- BlockDevice -----------------------------------###

class ThreadLocalAlignedBuffer:
    '''
    RAII wrapper around an aligned buffer allocated for each thread.
    Destructor frees the buffer when the weakref is finalized better than __del__
    since it even catches after interpreter shutdown.

    Current scratch preallocated: 64MB + 64KB
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
    A read-only, thread-safe read into an ISO file or raw optical drive, backed by block_device.c

    `path` is a regular file or a raw device ('\\\\.\\E:', '/dev/sr0', '/dev/rdisk4/).
    as a string and never round-tripped with pathlib (Path will mess up drive paths)
    Failures raise OSError codes.

    This is a thin wrapper: the native layer owns the I/O, the alignment, and it's own safety.
    Whoever owns the BlockDevice (PhysicalHandler) decides when to close it.
    The native struct is freed when this object is garbage collected.

    Reads go through a per-thread aligned scratch buffer, with larger reads falling back to a temp buffer.
    '''
    CLOSE_TIMEOUT_MS = 15000  # ms before giving up on a read, slow optical drives may need more time

    def __init__(self, path: str | bytes | Path, sector_size: int = 2048, encoding: str | None = None) -> None:
        self._encoding   = None
        if isinstance(path, (bytes, bytearray)):
            self._encoding = encoding or ('utf-8' if _IS_WINDOWS else sys.getfilesystemencoding())
            path = bytes(path).decode(self._encoding, errors='surrogateescape')
        self._path_str   = _normalise_path(str(path))
        self.path        = Path(self._path_str)
        self.sector_size = sector_size
        self.alignment   = max(sector_size, 2048)
        self.is_device   = False
        self._lib        = get_block_device_lib()
        self._size       = 0
        self._dev_handle: int | None = None
        self._tls        = threading.local()
        self.closed      = True

        # Not completely thread-safe, but good enough for reporting read stats
        self.scratch_reads = 0
        self.large_reads = 0

        self._open(sector_size)
        self.closed = False

    ###---------------------------------- open / close ----------------------------------###

    # Python codec name -> PathEncoding id in block_device.c
    _WIN_ENCODINGS = {'utf8': 0, 'cp932': 1, 'shiftjis': 1, 'sjis': 1, 'eucjp': 2, 'mbcs': 3}

    def _native_path(self) -> tuple[bytes, int]:
        '''
        (path bytes, PathEncoding id) for open_device.
        POSIX:    the bytes go straight to open(), id is ignored.
        Windows:  C converts the bytes to UTF-16 with the code page the id names (UTF-8 when unsupported)
        '''
        if not _IS_WINDOWS:
            codec = self._encoding
            return (self._path_str.encode(codec, 'surrogateescape') if codec else os.fsencode(self._path_str)), 0
        codec = (self._encoding or 'utf-8').lower().replace('-', '').replace('_', '')
        encoding_id = self._WIN_ENCODINGS.get(codec, 0)
        if encoding_id == 0:
            return self._path_str.encode('utf-8'), 0
        return self._path_str.encode(self._encoding, 'surrogateescape'), encoding_id

    def _open(self, sector_size: int) -> None:
        lib = self._lib
        raw_path, path_encoding = self._native_path()
        handle = lib.open_device(raw_path, sector_size, path_encoding)
        if not handle:
            raise _os_error(lib.get_last_error(), self._path_str)
        size = int(lib.get_device_size(handle))
        if size <= 0:
            lib.free_device(handle)
            raise OSError(f'{self._path_str} is empty or reports no size.')
        self._dev_handle = handle
        self._size       = size
        self.sector_size = int(lib.get_sector_size(handle))
        self.alignment   = max(self.sector_size, 2048)
        self.is_device   = bool(lib.is_raw_device(handle))
        weakref.finalize(self, lib.free_device, handle)

    def close(self) -> None:
        '''Idempotent. Stops new, in-flight, or waiting reads. Releases the handle.
        Never frees during operation.'''
        if self.closed:
            return
        self.closed = True
        handle = self._dev_handle
        if handle and self._lib.close_device(handle, self.CLOSE_TIMEOUT_MS):
            logger.error(f'{self._path_str}: a read did not stop within {self.CLOSE_TIMEOUT_MS}ms. Handle is realeased when it finishes...')
        self._tls = threading.local()
        logger.debug(
            f'Closed: {self.closed}, _dev_handle: {self._dev_handle}.\n'
            f'    Scratch reads: {self.scratch_reads}, Large reads: {self.large_reads}'
        )

    def reopen(self, sector_size: int) -> None:
        '''
        Reopen with the sector size the detected source needs. Called before concurrent use begins.
        The old native struct is only released here (not freed), so a stray
        late read fails cleanly instead of touching freed memory.
        '''
        if self.closed or not self._dev_handle:
            raise ValueError('Cannot reopen a closed BlockDevice')
        if sector_size == self.sector_size:
            return
        old = self._dev_handle
        self._lib.close_device(old, self.CLOSE_TIMEOUT_MS)
        self._tls = threading.local()
        try:
            self._open(sector_size)
        except BaseException:
            self.closed = True
            raise
        logger.debug(f'Reopened {self.name} with sector_size={self.sector_size} alignemnt={self.alignment}')

    def __enter__(self) -> BlockDevice:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _handle(self) -> int:
        if self.closed or not self._dev_handle:
            raise ValueError('I/O operation on closed BlockDevice')
        return self._dev_handle

    def _io_error(self, offset: int, size: int) -> Exception:
        '''The exception for a failed native read.'''
        code = int(self._lib.get_last_error())
        if code == _ABORTED_CODE:
            return InterruptedError(f'{self._path_str} was closed during a read.')
        err = _os_error(code, self._path_str, action=f'read {size} bytes at offset {offset:#x}')
        logger.error(f'Read of {size} bytes at {offset:#x} failed: {err}')
        return err

    ###------------------------------------ Reads -------------------------------------###

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
        handle = self._handle()
        size = min(size, self._size - offset)
        if size <= 0:
            return b''
        # Fetch the thread-local scratch buffer
        tls_buf = self._scratch_buffer
        if self._requires_large_read(offset, size, tls_buf.capacity): # dynamic buffer fallback
            return self._pread_large(handle, offset, size)
        skip_val = ctypes.c_size_t(0)
        # Get the slice
        available = self._lib.read_slice(
            handle,
            offset,
            size,
            tls_buf.ptr,
            tls_buf.capacity,
            ctypes.byref(skip_val)
        )
        if available < 0:
            raise self._io_error(offset, size)
        self.scratch_reads += 1
        return ctypes.string_at(tls_buf.ptr + skip_val.value, available) # Moving the ptr forward itself saves a copy

    def _pread_large(self, handle: int, offset: int, size: int) -> bytes:
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
                handle,
                aligned_offset,
                temp_buf,
                aligned_size
            )
            if bytes_read < 0:
                raise self._io_error(offset, size)
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
        handle = self._handle()
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
            handle, offset, size, tls_buf.ptr, tls_buf.capacity, ctypes.byref(skip_val)
        )
        if available < 0:
            raise self._io_error(offset, size)
        self.scratch_reads += 1
        c_array = (ctypes.c_char * available).from_address(tls_buf.ptr + skip_val.value)
        return memoryview(c_array)

    def readinto(self, buffer: memoryview | bytearray, offset: int) -> int:
        '''Zero-copy read into a pre-allocated python memoryview buffer.'''
        handle = self._handle()
        size = min(len(buffer), self._size - offset)
        if size <= 0:
            return 0
        tls_buf = self._scratch_buffer
        # Fast path, read into scratchpad
        if not self._requires_large_read(offset, size, tls_buf.capacity):
            view = self.pread_view(offset, size)
            count = len(view)
            buffer[:count] = view  # pread_view already counted the scratch read
            return count
        # Slow path, read into temp aligned C-buffer
        skip_bytes = offset & (self.sector_size - 1)
        aligned_offset = offset - skip_bytes
        aligned_size = (size + skip_bytes + self.sector_size - 1) & ~(self.sector_size - 1)
        temp_buf = self._lib.alloc_aligned_buffer(aligned_size, self.sector_size)
        if not temp_buf:
            raise MemoryError('Failed to allocate large aligned buffer')
        try:
            bytes_read = self._lib.read_sectors(handle, aligned_offset, temp_buf, aligned_size)
            if bytes_read < 0:
                raise self._io_error(offset, size)
            available = max(0, bytes_read - skip_bytes)
            available = min(available, size)
            self.large_reads += 1
            c_array = (ctypes.c_char * available).from_address(temp_buf + skip_bytes)
            buffer[:available] = c_array
            return available
        finally:
            self._lib.free_aligned_buffer(temp_buf)

    ###--------------------------------- Properties -------------------------------------###

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        '''Like Path.name, falling back to the raw string for a device path (Path('\\\\.\\E:').name is empty'''
        return self.path.name or self._path_str

    def __len__(self) -> int:
        return self._size

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f'<BlockDevice {self._path_str}>'
