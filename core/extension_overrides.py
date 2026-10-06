'''File Extension naming overrides'''
from functools import lru_cache

HEADERS = {
    # Core containers
    b'SLZ'    : '.slz',
    b'SLE'    : '.sle',
    b'ZLS'    : '.zls',
    b'Kods'   : '.kods',
    b'MWo3'   : '.mwo3',
    b'PACK'   : '.pack',
    b'1bcb'   : '.bcb',
    b'VIB'    : '.vib',
    (0x464C457F).to_bytes(4, 'little'): ".IRX", # changed to match the ISO files
    (0xD51556).to_bytes(3, 'little'): ".idx",   # TOC
    # Audio
    b'SEQW'   : '.seqw',
    b'VAGp'   : '.VAG',
    (0x000020).to_bytes(3, 'little'): ".020",
    (0x000010).to_bytes(3, 'little'): ".010",
    (0x000000).to_bytes(3, 'little'): ".000",
    # Movie
    (0x225277).to_bytes(3, 'little'): ".fmv",
    # Mesh
    b'FPS'    : '.fps',
    b'FSS'    : '.fss',
    b'IDOM'   : '.idom',
    b'LCTP'   : '.lctp',
    # Event
    b'EVD'    : '.evd',
    # Animation
    b'FAS'    : '.fas',
    b'HFAS'   : '.hfas',
    b'RMAC'   : '.rmac',
    b'RTA'    : '.rta',
    b'PAF'    : '.paf',
    # Texture
    b'FIS\00' : '.fis',
    b'FISP'   : '.fisp',
    b'FISA'   : ',fisa',
    b'TIM2'   : '.tim2',
    # Scene
    b'RBAD'   : '.rbad',
    b'RLF'    : '.rlf',
    b'RMF'    : '.rmf',
    b'NDNC'   : '.ndnc',
    b'XBDC'   : '.xbdc',
    b'PCDC'   : '.pcdc',
    b'DNAL'   : '.dnal',
    b'TGIL'   : '.tgil',
    # Gameplay
    b'0MPA'   : '.mpa',
    b'0DTH'   : '.dth',
    b'0CPA'   : '.cpa',
    b'0IPA'   : '.ipa',
    b'0FDC'   : '.fdc',
    # Unknown / Descriptor
    b'RCP'    : '.rcp',
    b'RCAD'   : '.rcad',
    (0x89504E47).to_bytes(4, 'big'): ".png",
    b'DTT'    : '.dtt',
    b'so3m'   : '.mclib',
    b'mcps'   : '.mclib1.5',
    b'DMY'    : '.dmy',
    b'DCM'    : '.dcm',
    b'CRC'    : '.crc',
}

@lru_cache(maxsize=1)
def generate_ext_overrides() -> dict[bytes, str]:
    """Build complete magic→extension map. Result is cached; callers must not mutate it."""
    overrides = {}

    for idx, name in HEADERS.items():
        overrides[idx] = name

    return overrides


@lru_cache(maxsize=1)
def _ext_prefix_index() -> dict[int, dict[bytes, str]]:
    """Build a length-bucketed prefix index from generate_ext_overrides().

    Buckets are ordered by the first time a given signature length is
    encountered when iterating the overrides dict (insertion order).  Because
    no two signatures of different lengths are prefixes of each other in the
    current table, bucket iteration order does not change which match wins —
    but the equivalence test in tests/test_ext_lookup.py is the authoritative
    guarantee.
    """
    d = generate_ext_overrides()
    index: dict[int, dict[bytes, str]] = {}
    for sig, ext in d.items():
        index.setdefault(len(sig), {})[sig] = ext
    return index


def lookup_extension(header: bytes, default: str = '.bin') -> str:
    """Return the extension for *header* using a prefix-index lookup.

    Equivalent to:
        next((m for s, m in generate_ext_overrides().items()
              if header.startswith(s)), default)
    but O(len(buckets)) hash lookups instead of O(n) linear scan.
    """
    for length, table in _ext_prefix_index().items():
        ext = table.get(header[:length])
        if ext is not None:
            return ext
    return default
