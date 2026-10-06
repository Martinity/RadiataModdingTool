'''
Registry; the global lookup for all handlers, editors, and sources.
Registration happens at startup catching errors before runtime and locks preventing runtime mutations.

HandlerProfile, EditorProfile are created for @Registry.register, @Registry.register_editor respectively.
SourceProfiles are created for @Registry.register_source.
FormatResolver features the lookup API

The registry supports multiple handlers or editors for the same category/extension. However the current codebase uses
get_handler_profile as a default. In the future if the tool grows and there is an interest in multiple handlers/editors
per category/extension, the codebase will need to be updated to get_handler_profiles.
'''
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from pathlib import Path
from core.workers import ActionDef, ActionType
from core.contracts import BaseSource, BaseEditor, BaseHandler
from core.handlers import discover_handlers
from ui.editors import discover_editors
from core.sources import discover_sources

if TYPE_CHECKING:
    from core.node import VfsNode
    from core.native.block_device import BlockDevice

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------- Globals ------------------------------------------###

GLOBAL_ACTIONS: tuple[ActionDef, ...] = (
    ActionDef('Export as Raw Bytes', ActionType.EXPORT),
    ActionDef('Import and Replace', ActionType.IMPORT),
)
_GLOBAL_ACTIONS_BY_NAME: dict[str, ActionDef] = {a.name: a for a in GLOBAL_ACTIONS}

###------------------------------------ Format Resolver --------------------------------------------###

@runtime_checkable
class FormatResolver(Protocol):
    '''Inject FormatResolver rather than importing Registry directly to decouple subsystems.'''
    def get_handler_profile(self, node: 'VfsNode') -> 'HandlerProfile | None': ...
    def get_handler_profiles(self, node: 'VfsNode') -> 'list[HandlerProfile]': ...
    def get_editor_profile(self, editor_class: 'type[BaseEditor]') -> 'type[EditorProfile] | None': ...
    def get_handler(self, source: 'VfsNode | Path') -> 'type[BaseHandler] | None': ...
    def get_editors(self, node: 'VfsNode') -> 'list[type[BaseHandler]]': ...
    def get_action(self, node: 'VfsNode', action_name: str) -> 'ActionDef | None': ...
    def get_handler_for_editor(self, editor: 'type[BaseEditor]') -> 'type[BaseHandler] | None': ...
    def get_source(self, source_id: str) -> 'BaseSource': ...
    def detect_source(self, handle: 'BlockDevice') -> 'BaseSource': ...

###---------------------------------------- Format Profiles ------------------------------------------------###

@dataclass(frozen=True)
class HandlerProfile:
    '''one profile to one handler. Owns extensions, categories, actions. everything needed for processing a node'''
    name:          str
    handler_class: type[BaseHandler]
    extensions:    tuple[str, ...]
    categories:    tuple[str, ...] = ()
    actions:       tuple[ActionDef, ...] = ()
    is_fallback:   bool = False

    _action_map: dict[str, ActionDef] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        '''builds the 0(1) lookup'''
        object.__setattr__(self, '_action_map', {a.name: a for a in self.actions})

    def get_action(self, name: str) -> ActionDef | None:
        return self._action_map.get(name)

    def primary_expand_action(self) -> ActionDef | None:
        return next(
            (a for a in self.actions if a.action_type == ActionType.TREE_EXPAND),
            None
        )

@dataclass(frozen=True)
class EditorProfile:
    '''one profile to one editor. Owns editor class, pairs handler, dispay metadata'''
    name:          str
    handler_class: type[BaseHandler]
    editor_class:  type[BaseEditor]
    extensions:    tuple[str, ...] = ()
    categories:    tuple[str, ...] = ()
    is_fallback:   bool = False

###------------------------------------------ Registry --------------------------------------------###

class Registry:
    '''
    Central format service locator. Supports two kinds of resolutions.
    Node resolution      get_profile(node) / get_handler(node)
    Editor resolution    get_handler_for_editor(editor)
    Source resolution    get_source(source_id) / detect_source(handle)
    '''
    _handler_profiles: list[HandlerProfile] = []
    _editor_profiles:  list[EditorProfile] = []
    _handler_by_ext:   dict[str, list[HandlerProfile]] = {}
    _handler_by_cat:   dict[str, list[HandlerProfile]] = {}
    _editor_by_ext:    dict[str, list[EditorProfile]] = {}
    _editor_by_cat:    dict[str, list[EditorProfile]] = {}
    _global_editors:   list[EditorProfile] = []
    _sources:          dict[str, BaseSource] = {}
    _locked:           bool = False

    @classmethod
    def lock(cls) -> None:
        '''freeze the registry. called after all plugins are loaded'''
        cls._locked = True
        registered_handlers = {p.handler_class for p in cls._handler_profiles}
        for editor_profile in cls._editor_profiles:
            if editor_profile.handler_class not in registered_handlers:
                raise ValueError(
                    f'EditorProfile {editor_profile.name!r} references handler '
                    f'{editor_profile.handler_class.__name__!r}, which has no '
                    f'@Registry.register profile of its own. prepare_editor_data '
                    f'will use BaseHandler defaults instead of this handler\'s overrides.'
                )
        logger.info(
            f'Locked -- {len(cls._handler_profiles)} handler(s), {len(cls._editor_profiles)} editor(s)'
            f'{len(cls._global_editors)} global editor(s), {len(cls._sources)} source(s)'
        )
        print(cls.summary())

    @classmethod
    def reset(cls) -> None:
        '''for testing'''
        cls._handler_profiles.clear()
        cls._editor_profiles.clear()
        cls._handler_by_ext.clear()
        cls._handler_by_cat.clear()
        cls._editor_by_ext.clear()
        cls._editor_by_cat.clear()
        cls._global_editors.clear()
        cls._sources.clear()
        cls._locked = False

    ###----------------------------------- register ------------------------------------------###
    @classmethod
    def register(
        cls,
        name:              str,
        extensions:        tuple[str, ...] = (),
        categories:        tuple[str, ...] = (),
        supported_actions: tuple[ActionDef, ...] | dict[str, ActionType] | None = None,
        is_fallback:       bool = False,
    ):
        '''
        Decorator for BaseHandler subclasses.

        supported_actions accepts:
            tuple[ActionDef, ...]   preferred ActionDef
            dict[str, ActionType]   shorthand get converted to ActionDef tuple
            None                    no format specific actions
        '''
        def decorator(cls_or_func):
            if cls._locked:
                raise RuntimeError(
                    f'Locked - Cannot register "{name}" after discover_all() has completed'
                    f'Ensure all plugins are imported inside discover_all()'
                )
            if not issubclass(cls_or_func, BaseHandler):
                raise TypeError(
                    f'@Registry.register is for BaseHandler subclasses only. Use @Registry.register_editor for BaseEditor. '
                    f'{cls_or_func.__name__!r} is not a BaseHandler.'
                )
            actions = _normalise_actions(supported_actions)
            cls_or_func._plugin_name = name

            profile = HandlerProfile(
                name=name,
                handler_class=cls_or_func,
                extensions=extensions,
                categories=categories,
                actions=actions,
                is_fallback=is_fallback,
            )
            cls._handler_profiles.append(profile)

            for ext in extensions:
                if ext not in cls._handler_by_ext or not profile.is_fallback:
                    cls._handler_by_ext.setdefault(ext, []).append(profile)
            for cat in categories:
                if cat not in cls._handler_by_cat or not profile.is_fallback:
                    cls._handler_by_cat.setdefault(cat, []).append(profile)

            return cls_or_func
        return decorator

    @classmethod
    def register_editor(
            cls,
            name:        str,
            handler:     type[BaseHandler],
            extensions:  tuple[str, ...] = (),
            categories:  tuple[str, ...] = (),
            is_fallback: bool = False,
    ):
        '''
        Decorator for BaseEditor subclasses

        handler= is required and is the actual handler class
        '''
        if not (isinstance(handler, type) and issubclass(handler, BaseHandler)):
            raise TypeError(
                f'register_editor "{name}": handler must be a BaseHandler subclass, got {handler!r}.'
            )

        def decorator(cls_or_func):
            if cls._locked:
                raise RuntimeError(
                    f'Locked -- cannot register editor "{name}" after discover_all()'
                )
            if not issubclass(cls_or_func, BaseEditor):
                raise TypeError(
                    f'@Registry.register_editor is for BaseEditor subclasses only. '
                    f'{cls_or_func.__name__!r} is not a BaseEditor.'
                )
            cls_or_func._plugin_name = name
            profile = EditorProfile(
                name=name,
                handler_class=handler,
                editor_class=cls_or_func,
                extensions=extensions,
                categories=categories,
                is_fallback=is_fallback,
            )
            cls._editor_profiles.append(profile)
            if is_fallback:
                cls._global_editors.append(profile)

            for ext in extensions:
                cls._editor_by_ext.setdefault(ext, []).append(profile)
            for cat in categories:
                cls._editor_by_cat.setdefault(cat, []).append(profile)

            return cls_or_func
        return decorator

    @classmethod
    def register_source(cls, source_cls: type[BaseSource]) -> type[BaseSource]:
        '''
        Decorator for BaseSource subclasses. Validates the definition, then keeps one instance.
        Errors surface here, at startup, naming the missing attribute or abstract method.
        '''
        if cls._locked:
            raise RuntimeError(
                f'Locked - Cannot register "{getattr(source_cls, "__name__", source_cls)}" after discover_all() '
                f'has completed. Ensure all plugins are imported inside discover_all()'
            )
        if not (isinstance(source_cls, type) and issubclass(source_cls, BaseSource)):
            raise TypeError(f'@Registry.register_source is for BaseSource subclasses only, got {source_cls!r}.')
        source_cls.validate_definition()
        if source_cls.display_name in cls._sources:
            raise ValueError(f'Source {source_cls.display_name!r} is already registered')
        cls._sources[source_cls.display_name] = source_cls()
        return source_cls

    ###------------------------------- Lookups ----------------------------------###
    @classmethod
    def get_handler_profile(cls, node: VfsNode) -> HandlerProfile | None:
        '''
        Returns best handler profile for a node.
        This should be only used for handlers that are uniquely registered to an extension.
        If used on an extension with multiple handlers, the first match is returned.
        '''
        profiles = cls.get_handler_profiles(node)
        return profiles[0] if profiles else None

    @classmethod
    def get_handler_profiles(cls, node: VfsNode) -> list[HandlerProfile]:
        '''
        Returns list of valid handler profiles for a node.
        Most of the time this will be the best way to get a handler profile for a node.
        '''
        matches: list[HandlerProfile] = []
        seen_names: set[str] = set()

        def _add(profile: HandlerProfile):
            if profile.name not in seen_names:
                matches.append(profile)
                seen_names.add(profile.name)

        if node.extension: # Gather extension matches
            for p in cls._handler_by_ext.get(node.extension, []):
                _add(p)
        if node.category: # Gather category matches
            for cat in node.category:
                if cat == 'Unknown':
                    continue
                for p in cls._handler_by_cat.get(cat, []):
                    _add(p)

        return matches

    @classmethod
    def get_handler(cls, source: VfsNode | Path) -> type[BaseHandler] | None:
        '''
        Return the first matching handler for the source.
        Performs platform-specific checks for physical drives to force SourceHandler.
        '''
        if isinstance(source, Path):
            import platform
            path_str = str(source)
            is_physical_drive = False
            if platform.system() == 'Windows':
                is_physical_drive = path_str.startswith(r'\\\\.\\')
            else:
                try:
                    is_physical_drive = source.exists() and (source.is_block_device() or source.is_char_device())
                except OSError:
                    pass  # Permissions errors are logged to the ui or features should be already disabled
            if is_physical_drive:
                # Force SourceHandler for physical drives
                p = cls._handler_by_ext.get('.iso')
                return p[0].handler_class if p else None
            if source.is_file():
                # Identify standard Path file extensions
                p = cls._handler_by_ext.get(source.suffix.lower())
                return p[0].handler_class if p else None
            # Not a valid Path
            return None
        profile = cls.get_handler_profile(source)
        return profile.handler_class if profile else None

    @classmethod
    def get_editors(cls, node: VfsNode) -> list[type[BaseEditor]]:
        '''Return all valid editors for a node, ordered by fallbacks last'''
        seen:    set[type[BaseEditor]] = set()
        editors: list[type[BaseEditor]] = []

        def _add(profile: EditorProfile) -> None:
            if profile.editor_class not in seen:
                seen.add(profile.editor_class)
                editors.append(profile.editor_class)

        if node.extension:
            for p in cls._editor_by_ext.get(node.extension, []):
                _add(p)
        if node.category:
            for cat in node.category:
                if cat == 'Unknown':
                    continue
                for p in cls._editor_by_cat.get(cat, []):
                    _add(p)
        for p in cls._global_editors:
            _add(p)
        return editors

    @classmethod
    def get_editor_profile(cls, editor_class: type[BaseEditor] | type) -> EditorProfile | None:
        '''Return the EditorProfile associated with a specific editor'''
        return next(
            (p for p in cls._editor_profiles if p.editor_class is editor_class),
            None,
        )

    @classmethod
    def get_handler_for_editor(cls, editor: 'BaseEditor') -> type[BaseHandler] | None:
        '''Return the handler declared by an editor's profile'''
        editor_cls = editor if isinstance(editor, type) else editor.__class__
        profile = cls.get_editor_profile(editor_cls)
        if not profile:
            logger.warning(f'{editor.__class__.__name__} has no EditorProfile - falling back to node handler')
            return None
        return profile.handler_class

    @classmethod
    def get_action(cls, node: VfsNode, action_name: str) -> ActionDef | None:
        '''Resolve ActionDef from action_name for a given node.
        Checks node's HandlerProfile first falling back to global actions.'''
        profile = cls.get_handler_profile(node)
        if profile:
            action = profile.get_action(action_name)
            if action:
                return action
        return _GLOBAL_ACTIONS_BY_NAME.get(action_name, None)

    @classmethod
    def get_source(cls, source_id: str) -> BaseSource:
        try:
            return cls._sources[source_id]
        except KeyError:
            raise ValueError(f'unknown source {source_id!r}. Available: {sorted(cls._sources)} from None')

    @classmethod
    def list_sources(cls) -> list[BaseSource]:
        return list(cls._sources.values())

    @classmethod
    def detect_source(cls, handle: BlockDevice) -> BaseSource:
        '''Return the registered source whose signature matches'''
        for source in cls._sources.values():
            if source.matches(handle):
                return source
        raise ValueError('No registered source matched this disk.')

    ###----------------------------------- Diagnostics ---------------------------------------###
    @classmethod
    def summary(cls) -> str:
        '''human-readable registration summary'''
        lines = [f'Registry (locked={cls._locked})']
        lines.append(f'  Handlers ({len(cls._handler_profiles)}):')
        for p in cls._handler_profiles:
            actions_str  = ', '.join(a.name for a in p.actions) or '-'
            lines.append(
                f'    {p.name!r:40s} - {p.handler_class.__name__:30s} ext={p.extensions} actions=[{actions_str}]'
            )
        lines.append(f'  Editors ({len(cls._editor_profiles)}):')
        for p in cls._editor_profiles:
            fallback = ' [global]' if p.is_fallback else ''
            lines.append(
                f'    {p.name!r:40s} - {p.editor_class.__name__:30s} handler={p.handler_class.__name__} '
                f'ext={p.extensions} role={p.editor_class!r}{fallback}'
            )
        lines.append(f'  Sources ({len(cls._sources)}):')
        for source in cls._sources.values():
            lines.append(
                f'    {source.display_name!r:30s} - {source.display_name:30s} sector_size={source.geometry.sector_size:#x} '
                f'builder={source.builder.__name__ if source.builder else "None"} patches=[{", ".join(p.name for p in source.patches)}]'
            )
        return '\n'.join(lines)

###---------------------------------------- Helpers-----------------------------------------###

def _normalise_actions(actions: tuple[ActionDef, ...] | dict[str, ActionType] | None) -> tuple[ActionDef, ...]:
    '''
    Convert supported_actions to a uniform tuple[ActionDef]
    tuple[ActionDef, ...]  returned as-is after validated
    dict[str, ActionType]  key/values become ActionDef[key, value]
    None                   returns empty tuple
    '''
    if not actions:
        return ()
    if isinstance(actions, (tuple, list)):
        for item in actions:
            if not isinstance(item, ActionDef):
                raise TypeError(f'supported_actions items must be ActionDef, got {type(item).__name__!r}')
        return tuple(actions)
    if isinstance(actions, dict):
        return tuple(ActionDef(name=k, action_type=v) for k, v in actions.items())
    raise TypeError(
        f'supported_actions must be tuple[ActionDef] or dict[str, ActionType], got {type(actions).__name__!r}'
    )


def discover_all() -> None:
    '''
    Import all handlers/editors and lock the registry.
    Catches all import errors and raises a RuntimeError if any are encountered.
    '''
    handler_errors = discover_handlers(Registry)
    editor_errors = discover_editors(Registry)
    source_errors = discover_sources(Registry)
    all_errors = handler_errors + editor_errors + source_errors

    if all_errors:
        import traceback
        error_count = len(all_errors)

        # Use python traceback to get detailed discovery registration errors.
        detailed_errors = []
        for mod, err in all_errors:
            if getattr(err, '__traceback__', None):
                tb_lines = traceback.format_exception(type(err), err, err.__traceback__)
                tb_str = ''.join(tb_lines).replace('\n', '\n      ').strip()
                detailed_errors.append(f'    {mod}:\n      {tb_str}')
            else:
                detailed_errors.append(f'    {mod}: {err}')
        error_details = '\n'.join(detailed_errors)
        fatal_msg = f'Application startup failed at discovery. Discovered {error_count} plugin errors:\n{error_details}'
        logger.critical(fatal_msg)
        raise RuntimeError(fatal_msg)
    Registry.lock()
    logger.info('Registry filled and locked.')
