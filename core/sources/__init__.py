'''
Imports all handler modules from the current directory.

Will raise errors from source module importing.
'''
import pkgutil
import importlib
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.registry import Registry as RegistryType

def discover_sources(registry: 'type[RegistryType]') -> list[tuple[str, Exception]]:
    """Dynamically import all sources."""
    errors: list[tuple[str, Exception]] = []
    module_names: list[str] = []

    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        module_names.append(f'{__name__}.{module_info.name}')

    if not module_names:
        errors.append((__name__, RuntimeError(
            f'{__name__}: pkgutil.iter_modules found no source modules to import.'
        )))
        return errors

    for full_name in module_names:
        before = len(registry._sources)
        try:
            importlib.import_module(full_name)
        except Exception as e:
            errors.append((full_name, e))
            continue

        after = len(registry._sources)
        if after == before:
            errors.append((full_name, RuntimeError(
                f'{full_name} imported successfully but found no profile to register. '
                f'Ensure that the source is defined with @Registry.register_source decorator.'
            )))

    return errors
