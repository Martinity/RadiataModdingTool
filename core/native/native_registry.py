'''
Native Library Registry for all native libraries that are used by the modding tool.
Handles tiered resolution, JIT compliation, dependency discovery, and thread-safe loading.
'''
from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_INCLUDE_REGEX = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.MULTILINE)

@dataclass(frozen=True)
class NativeLibrary:
    '''Defines the root of a native library, including its name, path, and dependencies.'''
    name:      str
    root_dir:  Path
    sources:   list[str]
    link_args: list[str] = field(default_factory=list)
    cflags:    list[str] = field(default_factory=list)

class NativeRegistry:
    '''Singleton registry for discovering, building, and binding C libraries.'''
    _cache: dict[str, ctypes.CDLL | None] = {}
    _locks: dict[str, threading.Lock] = {}
    _global_lock = threading.Lock()
    _BUILD_ROOT = (Path(__file__).resolve().parent.parent.parent / 'native_build').resolve()

    @classmethod
    def load(
        cls,
        module: NativeLibrary,
        bind_callback: Callable[[ctypes.CDLL], ctypes.CDLL]
    ) -> ctypes.CDLL | None:
        '''
        Two tiered native library resolution:
            1. Prebuilt - frozen build or precompiled library
            2. JIT compilation - first run from source (cached)
        '''
        with cls._global_lock:
            if module.name not in cls._locks:
                cls._locks[module.name] = threading.Lock()
        with cls._locks[module.name]:
            if module.name in cls._cache:
                return cls._cache[module.name]
            # First tier: Prebuilt
            lib = cls._try_load_prebuilt(module, bind_callback)
            # Second tier: JIT
            if not lib and not getattr(sys, 'frozen', False):
                lib = cls._try_compile_and_load(module, bind_callback)
            # Failed resolution
            if not lib:
                raise RuntimeError(f'Native library "{module.name}" not found and failed to compile.')
            cls._cache[module.name] = lib
            return lib

    ###--------------------------------- Dependency Resolution --------------------------###

    @classmethod
    def _resolve_dependencies(cls, module: NativeLibrary) -> set[Path]:
        '''Recursively scan sources for local #includes to find required headers.'''
        tracked: set[Path] = set()
        queue: list[Path] = [module.root_dir / src for src in module.sources]

        while queue:
            file_path = queue.pop(0)
            if file_path in tracked:
                continue
            tracked.add(file_path)
            if file_path.suffix not in ('.c', '.h'):
                continue
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                for match in _INCLUDE_REGEX.finditer(content):
                    header_name = match.group(1)
                    header_path = file_path.parent / header_name
                    if header_path.exists() and header_path not in tracked:
                        queue.append(header_path)
            except Exception as e:
                logger.debug(f'Failed to scan {file_path.name} for dependencies: {e}')
        return tracked

    ###--------------------------------- Build Logic --------------------------------###

    @classmethod
    def _library_filename(cls, name: str) -> str:
        '''Platform-specific library filename.'''
        if sys.platform.startswith('win'):
            return f'{name}.dll'
        if sys.platform == 'darwin':
            return f'lib{name}.dylib'
        return f'lib{name}.so'

    @classmethod
    def _platform_tag(cls) -> tuple[str, str]:
        '''Platform tag for the current runtime.'''
        if sys.platform.startswith('win'):
            osname = 'windows'
        elif sys.platform == 'darwin':
            osname = 'macos'
        else:
            osname = 'linux'
        return osname, platform.machine().lower()

    @classmethod
    def _find_c_compiler(cls) -> str | None:
        '''Find the C compiler for the current platform.'''
        if sys.platform.startswith('win'):
            return shutil.which('gcc') or shutil.which('clang')
        return shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')

    @classmethod
    def _build_needed(cls, library_path: Path, tracked_files: set[Path], force: bool) -> bool:
        '''Determine if a library needs to be built.'''
        if force or not library_path.exists():
            return True
        library_mtime = library_path.stat().st_mtime
        return any(src.stat().st_mtime > library_mtime for src in tracked_files)

    @classmethod
    def _try_compile_and_load(
        cls,
        module: NativeLibrary,
        bind_callback: Callable[[ctypes.CDLL], ctypes.CDLL]
    ) -> ctypes.CDLL | None:
        '''
        Attempt to compile the given native library and load it.
        Compilation will report any errors and warnings appropriately with the logger.
        Cache directory is matched to the build_native directory.
        '''
        try:
            tracked_files = cls._resolve_dependencies(module)
            missing = [src for src in module.sources if not (module.root_dir / src).exists()]
            if missing:
                logger.error(f'Missing required sources for {module.name}: {missing}')
                return None
            cls._BUILD_ROOT.mkdir(parents=True, exist_ok=True)
            library_name = cls._library_filename(module.name)
            library_path = cls._BUILD_ROOT / library_name

            if not cls._build_needed(library_path, tracked_files, force=False):
                return bind_callback(ctypes.CDLL(str(library_path)))

            compiler = cls._find_c_compiler()
            if not compiler:
                logger.error('No C compiler found on PATH.')
                return None

            if library_path.exists():
                try:
                    library_path.unlink()
                except OSError:
                    pass

            # Compile
            platform_flags = ['-static-libgcc'] if sys.platform.startswith('win') else ['-fPIC']
            cmd = [
                compiler,
                '-shared',
                '-O2',
                '-std=c99',
                '-Wall',
                '-Wextra',
                '-Wno-unused-command-line-argument', # Silence unused gcc flags for clang
                *platform_flags,
                *module.cflags,
                '-o',
                str(library_path)
            ]
            for src in module.sources:
                cmd.append(str(module.root_dir / src))
            cmd.extend(module.link_args)
            result = subprocess.run(cmd, cwd=module.root_dir, text=True, capture_output=True, check=False)
            build_output = f'{result.stderr or ""}\n{result.stdout or ""}'.strip()
            if result.returncode != 0:
                logger.error(f'Failed to build {module.name}: {build_output}')
                return None
            elif build_output:
                logger.warning(f'Compiler warnings for {module.name}:\n{build_output}')
            else:
                logger.info(f'Successfully built {module.name}.')
            return bind_callback(ctypes.CDLL(str(library_path)))
        except Exception as e:
            logger.warning(f'Failed to build native library {module.name}: {e}', exc_info=True)
            return None

    ###------------------------------------ Prebuilt Logic ------------------------------------###

    @classmethod
    def _try_load_prebuilt(
        cls,
        module: NativeLibrary,
        bind_callback: Callable[[ctypes.CDLL], ctypes.CDLL]
    ) -> ctypes.CDLL | None:
        '''Attempt to load a prebuilt native library from the build root to match the github actions build.'''
        name = cls._library_filename(module.name)
        osname, arch = cls._platform_tag()
        dirs: list[Path] = [cls._BUILD_ROOT]
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            base = Path(meipass)
            dirs += [base / 'native', base]
        root = module.root_dir
        dirs += [root / 'prebuilt' / f'{osname}-{arch}', root / 'prebuilt' / osname]
        for candidate in [d / name for d in dirs]:
            if candidate.exists():
                try:
                    lib = bind_callback(ctypes.CDLL(str(candidate)))
                    logger.debug(f'Loaded prebuild native library: {candidate.name}')
                    return lib
                except Exception as e:
                    logger.warning(f'Failed to load prebuilt library {module.name}: {e}')
        return None
