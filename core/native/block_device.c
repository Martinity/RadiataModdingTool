/**
 * Cross-platform block device OS I/O API wrapper built specifically for PhysicalHandler.
 * Ensures that memory is sector aligned with VirtualAlloc/posix_memalign.
 * uint64_t used to ensure 64-bit alignment on all platforms.
 * Reads are stateless and thread-safe meaning that the python context manager overhead on every
 * physical read is avoided.
 *
 *  Optical / raw devices:
 *      - Reads are whole device sectors: offset, length and buffer are sector aligned
 *      - Reads larger than RAW_MAX_IO are looped as several shorter reads
 *      - Sector size and capacity comes from polling the device itself
 *      - OS error codes propogate to the caller (get_last_error)
 *
 *  Lifetime (device manages it's own safety, the caller only calls these)
 *      close_device(): refuses new reads, aborts running ones, waits for them, releases the OS handle.
 *      free_device():  frees the struct. Called once nothing can reference the device any more
 *
 *  Windows: paths are converted to UTF-16 from the code page the caller names
 *  MacOS:   open the raw node /dev/rdiskN
 *  Linux:   open the block device /dev/sr0
 */
#define _GNU_SOURCE
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>

#if defined(_WIN32)
    #include <windows.h>
    #include <winioctl.h>
#else
    #include <time.h>
    #include <sys/types.h>
    #include <sys/stat.h>
    #include <fcntl.h>
    #include <sys/ioctl.h>
    #include <unistd.h>
    #if defined(__APPLE__)
        #include <sys/disk.h>
    #else
        #include <linux/fs.h>
    #endif
#endif

#define SECTOR_SIZE  2048       // Default sector alignment (ISO 9660 standard)
#define RAW_MAX_IO  (1u << 20)  // 1 MiB max individual read for raw devices
#define FILE_MAX_IO (64u << 20) // 64 MiB max individual read for regular files
#define READ_RETRIES 2          // Retries for transient device errors (not media errors)

#if defined(_WIN32)
    #define EXPORT __declspec(dllexport)
#else
    #define EXPORT __attribute__((visibility("default")))
#endif

#if defined(__GNUC__) || defined(__clang__)
    #define THREAD_LOCAL __thread
#else
    #define THREAD_LOCAL __declspec(thread)
#endif

//---------------------------------- Type Definitions ----------------------------------//

// Character encodings supported for Windows path conversion
typedef enum {
    PATH_ENCODING_UTF8,
    PATH_ENCODING_CP932,
    PATH_ENCODING_EUC_JP,
    PATH_ENCODING_ACP,
} PathEncoding;

// Native block device handle structure managed by python via ctypes
typedef struct {
#if defined(_WIN32)
    HANDLE handle;
#else
    int fd;
#endif
    int64_t  size;              // Total usable device or file size in bytes
    uint32_t sector_size;       // Effective I/O alignment requested/used
    uint32_t dev_sector_size;   // hardware reported physical sector size
    size_t   max_io;            // Largest allowed single transfer size
    int32_t  is_raw;            // 1 = raw device (sector aligned I/O only), 0 = regular file
    volatile int32_t closing;   // Non-zero if close/cancellation has been requested
    volatile int32_t readers;   // Active reader thread count inside read_sectors
    volatile int32_t os_closed; // Non-zero if the underlying OS handle has been released
} NativeBlockDevice;

//------------------------------ Errors & Thread Flags -----------------------------//

// OS errors are thread-local and must be retrieved immediately after a failure using get_last_error()
static THREAD_LOCAL int32_t g_last_error = 0;

#if defined(_WIN32)
    #define LAST_OS_ERROR()  ((int32_t)GetLastError())
    #define ERR_INVALID      ((int32_t)ERROR_INVALID_PARAMETER)
    #define ERR_NOMEM        ((int32_t)ERROR_NOT_ENOUGH_MEMORY)
    #define ERR_ABORTED      ((int32_t)ERROR_OPERATION_ABORTED)
    #define ERR_NO_MEDIA     ((int32_t)ERROR_NOT_READY)
    #define FLAG_SET(p)      InterlockedExchange((volatile LONG*)(p), 1)
    #define FLAG_GET(p)      InterlockedCompareExchange((volatile LONG*)(p), 0, 0)
    #define ATOMIC_INC(p)    InterlockedIncrement((volatile LONG*)(p))
    #define ATOMIC_DEC(p)    InterlockedDecrement((volatile LONG*)(p))
    #define FLAG_TAKE(p)     InterlockedExchange((volatile LONG*)(p), 1)
    #define NOW_MS()         ((uint64_t)GetTickCount64())
    #define SLEEP_MS(ms)     Sleep(ms)
#else
    #define LAST_OS_ERROR()  ((int32_t)errno)
    #define ERR_INVALID      ((int32_t)EINVAL)
    #define ERR_NOMEM        ((int32_t)ENOMEM)
    #define ERR_ABORTED      ((int32_t)ECANCELED)
    #if defined(ENOMEDIUM)
        #define ERR_NO_MEDIA ((int32_t)ENOMEDIUM)
    #else
        #define ERR_NO_MEDIA ((int32_t)ENXIO)
    #endif
    #define FLAG_SET(p)      __atomic_store_n((p), 1, __ATOMIC_SEQ_CST)
    #define FLAG_GET(p)      __atomic_load_n((p), __ATOMIC_SEQ_CST)
    #define ATOMIC_INC(p)    __atomic_add_fetch((p), 1, __ATOMIC_SEQ_CST)
    #define ATOMIC_DEC(p)    __atomic_sub_fetch((p), 1, __ATOMIC_SEQ_CST)
    #define FLAG_TAKE(p)     __atomic_exchange_n((p), 1, __ATOMIC_SEQ_CST)
    #define SLEEP_MS(ms)     usleep((useconds_t)(ms) * 1000u)
#endif

EXPORT int32_t get_last_error(void) {
    return g_last_error;
}

//------------------------------ Aligned Allocation ---------------------------------//

EXPORT void* alloc_aligned_buffer(size_t size, uint32_t alignment) {
    if (alignment == 0) alignment = SECTOR_SIZE;
#if defined(_WIN32)
    (void)alignment; // VirtualAlloc is inherently page-aligned
    return VirtualAlloc(NULL, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
#else
    void* ptr = NULL;
    if (posix_memalign(&ptr, alignment, size) != 0) return NULL;
    return ptr;
#endif
}

EXPORT void free_aligned_buffer(void* ptr) {
    if (!ptr) return;
#if defined(_WIN32)
    VirtualFree(ptr, 0, MEM_RELEASE);
#else
    free(ptr);
#endif
}

//------------------------------- Windows Platform Helpers --------------------------//

#if defined(_WIN32)

static THREAD_LOCAL HANDLE tls_event = NULL;

static HANDLE thread_event(void) {
    if (!tls_event) tls_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    return tls_event;
}

static BOOL ioctl_call(HANDLE h, DWORD code, void* out, DWORD out_size) {
    OVERLAPPED ov;
    DWORD returned = 0;
    HANDLE event = thread_event();
    if (!event) return FALSE;
    memset(&ov, 0, sizeof(ov));
    ov.hEvent = event;
    ResetEvent(event);
    if (!DeviceIoControl(h, code, NULL, 0, out, out_size, NULL, &ov) && GetLastError() != ERROR_IO_PENDING)
        return FALSE;
    return GetOverlappedResult(h, &ov, &returned, TRUE);
}

static int is_no_media_error(DWORD e) {
    // Errors that cannot be retried and hard stop the device
    return e == ERROR_NOT_READY || e == ERROR_NO_MEDIA_IN_DRIVE ||
           e == ERROR_UNRECOGNIZED_MEDIA || e == ERROR_DEVICE_NOT_CONNECTED;
}

static int is_transient_error(DWORD e) {
    // Errors that can be retried
    return e == ERROR_NOT_READY || e == ERROR_IO_DEVICE || e == ERROR_SEM_TIMEOUT;
}

static DWORD check_media(HANDLE h) {
    // Returns 0 when device media is present or uncertain, otherwise error
    DWORD err = 0;
    int attempt;
    for (attempt = 0; attempt < 2; attempt++) {
        if (ioctl_call(h, IOCTL_STORAGE_CHECK_VERIFY, NULL, 0)) return 0;
        err = GetLastError();
        if (err == ERROR_MEDIA_CHANGED) continue;
        break;
    }
    return is_no_media_error(err) ? err : 0;
}

static int64_t query_raw_size(HANDLE h, uint32_t* hw_sector) {
    // Capacity of the raw volume and its hardware sector size (0 if the device doesn't report one)
    GET_LENGTH_INFORMATION length_info;
    ULONGLONG geometry_buf[64];
    int64_t length = 0, geometry_size = 0;
    *hw_sector = 0;
    memset(&length_info, 0, sizeof(length_info));
    memset(geometry_buf, 0, sizeof(geometry_buf));
    if (ioctl_call(h, IOCTL_DISK_GET_LENGTH_INFO, &length_info, sizeof(length_info)))
        length = length_info.Length.QuadPart;
    if (ioctl_call(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, geometry_buf, sizeof(geometry_buf))) {
        const DISK_GEOMETRY_EX* geometry = (const DISK_GEOMETRY_EX*)geometry_buf;
        geometry_size = geometry->DiskSize.QuadPart;
        *hw_sector = geometry->Geometry.BytesPerSector;
    }
    return length > 0 ? length : geometry_size;
}

static UINT path_encoding_codepage(PathEncoding encoding) {
    switch (encoding) {
        case PATH_ENCODING_UTF8:   return CP_UTF8;
        case PATH_ENCODING_CP932:  return 932;
        case PATH_ENCODING_EUC_JP: return 51932;
        case PATH_ENCODING_ACP:    return CP_ACP;
        default:                   return 0;
    }
}

static wchar_t* path_to_wide(const char* path_str, PathEncoding encoding) {
    // Convert path bytes in the names encoding to UTF-16 for wide Win32 API. Caller frees the result
    UINT codepage;
    DWORD flags;
    int count;
    wchar_t* path_wide;

    if (path_str == NULL) return NULL;
    codepage = path_encoding_codepage(encoding);
    if (codepage == 0) return NULL;
    flags = (codepage == CP_UTF8 || codepage == 932) ? MB_ERR_INVALID_CHARS : 0;
    count = MultiByteToWideChar(codepage, flags, path_str, -1, NULL, 0);
    if (count <= 0) return NULL;
    if ((size_t)count > SIZE_MAX / sizeof(*path_wide)) return NULL;
    path_wide = (wchar_t*)malloc((size_t)count * sizeof(wchar_t));
    if (!path_wide) return NULL;
    if (MultiByteToWideChar(codepage, flags, path_str, -1, path_wide, count) == 0) {
        free(path_wide);
        return NULL;
    }
    return path_wide;
}

static int is_device_path(const wchar_t* path) {
    // \\.\E:  \\.\CdRom0  \\?\Volume{...} are devices, else is a file
    if (wcsncmp(path, L"\\\\.\\", 4) != 0 && wcsncmp(path, L"\\\\?\\", 4) != 0) return 0;
    return wcschr(path + 4, L'\\') == NULL;
}

static int open_platform(NativeBlockDevice* device, const char* path, PathEncoding encoding, int32_t* err) {
    DWORD previous_mode = 0;
    BOOL  mode_set = FALSE;
    wchar_t* wide_path = path_to_wide(path, encoding);
    LARGE_INTEGER li;

    if (!wide_path) {
        *err = path_encoding_codepage(encoding) ? (int32_t)ERROR_NO_UNICODE_TRANSLATION : ERR_INVALID;
        return -1;
    }
    // Suppress critical error dialogs
    mode_set = SetThreadErrorMode(SEM_FAILCRITICALERRORS, &previous_mode);
    device->handle = CreateFileW(
        wide_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, // Potential for FILE_FLAG_NO_BUFFERING
        NULL
    );
    if (device->handle == INVALID_HANDLE_VALUE) {
        *err = LAST_OS_ERROR();
        free(wide_path);
        if (mode_set) SetThreadErrorMode(previous_mode, NULL);
        return -1;
    }
    device->is_raw = is_device_path(wide_path);
    free(wide_path);
    if (device->is_raw) {
        DWORD media_err = check_media(device->handle);
        if (media_err) {
            *err = (int32_t)media_err;
            CloseHandle(device->handle);
            if (mode_set) SetThreadErrorMode(previous_mode, NULL);
            return -1;
        }
        device->size   = query_raw_size(device->handle, &device->dev_sector_size);
        device->max_io = RAW_MAX_IO;
    } else {
        device->size   = GetFileSizeEx(device->handle, &li) ? li.QuadPart : 0;
        device->max_io = FILE_MAX_IO;
    }
    if (mode_set) SetThreadErrorMode(previous_mode, NULL);
    return 0;
}

static void close_platform(NativeBlockDevice* device) {
    if (device->handle && device->handle != INVALID_HANDLE_VALUE) CloseHandle(device->handle);
}

static int64_t read_chunk(NativeBlockDevice* device, uint64_t offset, void* buffer, size_t count) {
    // One transfer. Uses OVERLAPPED for atomic stateless reads. 0 = eof, -1 = error, else count
    HANDLE event = thread_event();
    int attempt = 0;
    if (!event) {
        g_last_error = ERR_NOMEM;
        return -1;
    }
    for (;;) {
        OVERLAPPED ov;
        DWORD transferred = 0, err = 0;
        memset(&ov, 0, sizeof(ov));
        ov.Offset     = (DWORD)(offset & 0xFFFFFFFFu);
        ov.OffsetHigh = (DWORD)(offset >> 32);
        ov.hEvent     = event;
        ResetEvent(event);
        if (!ReadFile(device->handle, buffer, (DWORD)count, NULL, &ov)) {
            err = GetLastError();
            if (err != ERROR_IO_PENDING) goto failed;
        }
        // Requests must go through GetOverlappedResult on windows
        if (GetOverlappedResult(device->handle, &ov, &transferred, TRUE)) return (int64_t)transferred;
        err = GetLastError();
failed:
        if (err == ERROR_HANDLE_EOF) return 0;
        if (attempt < READ_RETRIES && is_transient_error(err) && !FLAG_GET(&device->closing)) {
            Sleep(100u * (DWORD)(attempt + 1));
            attempt++;
            continue;
        }
        g_last_error = (int32_t)err;
        return -1;
    }
}

//------------------------------------ POSIX Platform Helpers -----------------------------------//

#else

static int open_platform(NativeBlockDevice* device, const char* path, PathEncoding encoding, int32_t* err) {
    struct stat st;
    (void)encoding;  // Unused on POSIX
    device->fd = open(path, O_RDONLY | O_CLOEXEC);
    if (device->fd < 0) {
        *err = LAST_OS_ERROR();
        return -1;
    }
    device->is_raw = (fstat(device->fd, &st) == 0) && (S_ISBLK(st.st_mode) || S_ISCHR(st.st_mode));
    device->max_io = device->is_raw ? RAW_MAX_IO : FILE_MAX_IO;

#if defined(__APPLE__)
    {
        uint64_t block_count = 0;
        uint32_t block_size = 0;
        if (ioctl(device->fd, DKIOCGETBLOCKCOUNT, &block_count) == 0 &&
            ioctl(device->fd, DKIOCGETBLOCKSIZE,  &block_size)  == 0 && block_size)
        {
            device->size = (int64_t)(block_count * block_size);
            device->dev_sector_size = block_size;
#if defined(DKIOCGETMAXBYTECOUNTREAD)
            {
                uint64_t max_read = 0;
                if (device->is_raw && ioctl(device->fd, DKIOCGETMAXBYTECOUNTREAD, &max_read) == 0 &&
                    max_read >= block_size && max_read < device->max_io)
                {
                    device->max_io = (size_t)(max_read - (max_read % block_size));
                }
            }
#endif
        }
    }
#else
    {
        uint64_t bytes = 0;
        int logical = 0;
        if (ioctl(device->fd, BLKGETSIZE64, &bytes) == 0) device->size = (int64_t)bytes;
        if (ioctl(device->fd, BLKSSZGET, &logical) == 0 && logical > 0) device->dev_sector_size = (uint32_t)logical;
    }
#endif

    if (device->size <= 0) {
        off_t end = lseek(device->fd, 0, SEEK_END);
        device->size = (end >= 0) ? (int64_t)end : 0;
        lseek(device->fd, 0, SEEK_SET);
    }
    return 0;
}

static void close_platform(NativeBlockDevice* device) {
    if (device->fd >= 0) close(device->fd);
}

static int64_t read_chunk(NativeBlockDevice* device, uint64_t offset, void* buffer, size_t count) {
    // One transfer. Returns bytes read, 0 at end of media, or -1 with set g_last_error set
    for (;;) {
        // pread for atomic stateless unix reads
        ssize_t transferred = pread(device->fd, buffer, count, (off_t)offset);
        if (transferred >= 0) return (int64_t)transferred;
        if (errno == EINTR) continue;
        g_last_error = LAST_OS_ERROR();
        return -1;
    }
}
#endif

//---------------------------------- Lifecycle -------------------------------------//

static int is_power_of_2(uint32_t number) {
    return number != 0 && (number & (number - 1)) == 0;
}

static void settle_geometry(NativeBlockDevice* device) {
    uint32_t hardware = device->dev_sector_size;
    if (hardware && is_power_of_2(hardware)) {
        if (device->sector_size % hardware) {
            device->sector_size = device->sector_size > hardware ? device->sector_size : hardware;
        }
        device->size -= device->size % hardware;
    }
}

EXPORT NativeBlockDevice* open_device(const char* path, uint32_t sector_size, int32_t encoding) {
    NativeBlockDevice* device;
    int32_t err = 0;
    if (!path) {
        g_last_error = ERR_INVALID;
        return NULL;
    }
    device = (NativeBlockDevice*)calloc(1, sizeof(*device));
    if (!device) {
        g_last_error = ERR_NOMEM;
        return NULL;
    }
    if (sector_size == 0) sector_size = SECTOR_SIZE;
    device->sector_size = sector_size;
    if (open_platform(device, path, (PathEncoding)encoding, &err) != 0) {
        free(device);
        g_last_error = err;
        return NULL;
    }
    if (device->is_raw) {
        settle_geometry(device);
        if (device->size <= 0) {  // device exists but has no content
            close_platform(device);
            free(device);
            g_last_error = ERR_NO_MEDIA;
            return NULL;
        }
    }
    return device;
}

// Stop the device and release the OS handle:
//  1. Raise `closing` subsequent reads are refused and running reads stop between chunks
//  2. Windows cancel blocked overlapped reads that would be missed by CancelIoEx
//  3. Wait until no thread is inside read_sectors then release the handle.
// Return 0 once released, 1 if a read was stuck after timeout, which triggers free_device to release it.
// Sage to call from any thread. Never frees the struct.
EXPORT int32_t close_device(NativeBlockDevice* device, uint32_t timeout_ms) {
    uint64_t deadline;
    if (!device) return 0;
    FLAG_SET(&device->closing);

#if defined(_WIN32)
    deadline = NOW_MS() + timeout_ms;
#else
    {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        deadline = (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u + timeout_ms;
    }
#endif
    for (;;) {
#if defined(_WIN32)
        if (!FLAG_GET(&device->os_closed)) CancelIoEx(device->handle, NULL);
#endif
        if (FLAG_GET(&device->readers) == 0) break;
#if defined(_WIN32)
        if (NOW_MS() >= deadline) return 1;
#else
        {
            struct timespec ts;
            clock_gettime(CLOCK_MONOTONIC, &ts);
            if ((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u >= deadline) return 1;
        }
#endif
        SLEEP_MS(1);
    }
    if (FLAG_TAKE(&device->os_closed) == 0) close_platform(device);
    return 0;
}

// Free the struct. Valid only once nothing references the device.
// If a read is somehow active the struct is leaked rather than freed.
EXPORT void free_device(NativeBlockDevice* device) {
    if (!device) return;
    close_device(device, 1000);
    if (FLAG_GET(&device->readers) == 0 && FLAG_GET(&device->os_closed)) free(device);
}

//------------------------------------ Queries -------------------------------------//

EXPORT int64_t get_device_size(NativeBlockDevice* device) {
    return device ? device->size : 0;
}

EXPORT uint32_t get_sector_size(NativeBlockDevice* device) {
    return device ? device->sector_size : SECTOR_SIZE;
}

EXPORT uint32_t get_device_sector_size(NativeBlockDevice* device) {
    return device ? device->dev_sector_size : 0;
}

EXPORT int32_t is_raw_device(NativeBlockDevice* device) {
    return device ? device->is_raw : 0;
}

//--------------------------------- Stateless-Reads --------------------------------//

static int64_t read_loop(NativeBlockDevice* device, uint64_t byte_offset, void* out_buffer, size_t byte_count) {
    // Fill the buffer in chunks of at most max_io bytes. Stops early at end of media or when closing.
    size_t done = 0;
    size_t max_io = device->max_io ? device->max_io : RAW_MAX_IO;
    while (done < byte_count) {
        size_t want = byte_count - done;
        int64_t count;
        if (FLAG_GET(&device->closing)) {
            g_last_error = ERR_ABORTED;
            return -1;
        }
        if (want > max_io) want = max_io;
        count = read_chunk(device, byte_offset + done, (uint8_t*)out_buffer + done, want);
        if (count < 0) return -1;
        if (count == 0) break;
        done += (size_t)count;
    }
    return (int64_t)done;
}

EXPORT int64_t read_sectors(NativeBlockDevice* device, uint64_t byte_offset, void* out_buffer, size_t byte_count) {
    int64_t result;
    if (!device || !out_buffer || byte_count == 0) {
        g_last_error = ERR_INVALID;
        return -1;
    }
    // Register as a read before reading `closing`. close_device raises `closing` before `readers` read.
    // So either this read sees the flag and backs out, or close_device sees this read and waits for it.
    ATOMIC_INC(&device->readers);
    if (FLAG_GET(&device->closing)) {  // Refuse reads once a close was requested
        ATOMIC_DEC(&device->readers);
        g_last_error = ERR_ABORTED;
        return -1;
    }
    result = read_loop(device, byte_offset, out_buffer, byte_count);
    ATOMIC_DEC(&device->readers);
    return result;
}
//---------------------------------- Convenience -----------------------------------//

// Read the bytes [offset, offset+size) through a caller supplied aligned buffer (no malloc/free per read).
// The read itself is widened to whole sectors. *skip is where the requested bytes start inside the buffer.
// Returns the number of requested bytes available (short at end of media), or -1 with g_last_error set.
EXPORT int64_t read_slice(
    NativeBlockDevice* device,
    uint64_t offset,
    size_t size,
    void* aligned_buffer,    // Passed from python
    size_t buffer_capacity,  // Safety bounds
    size_t* skip             // Start offset of the requested bytes inside the buffer
)
{
    uint32_t sector_size;
    uint64_t mask, aligned_offset, remaining;
    size_t aligned_skip, aligned_size;
    int64_t count;
    size_t available = 0;

    if (!device || !aligned_buffer || !skip || size == 0) {
        g_last_error = ERR_INVALID;
        return -1;
    }
    // Calculate the requested slice from the sector read
    sector_size    = device->sector_size ? device->sector_size : SECTOR_SIZE;
    mask           = sector_size - 1;
    aligned_offset = offset & ~mask;
    aligned_skip   = (size_t)(offset & mask);
    aligned_size   = (size_t)((size + aligned_skip + mask) & ~(mask));

    if (aligned_offset >= (uint64_t)device->size) {
        *skip = 0;
        return 0;
    }
    // Bounds clamping
    remaining = (uint64_t)(device->size - aligned_offset);
    if (aligned_size > remaining) aligned_size = (size_t)remaining;
    // Capacity check
    if (aligned_size > buffer_capacity) {
        g_last_error = ERR_INVALID;
        return -1;
    }
    count = read_sectors(device, aligned_offset, aligned_buffer, aligned_size);
    if (count < 0) return -1;
    *skip = aligned_skip;
    if ((size_t)count > aligned_skip) available = (size_t)count - aligned_skip;
    if (available > size) available = size;
    return (int64_t)available;
}
