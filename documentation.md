# Radiata Modding Tool: Plugin Developer API

__Temporarily out of date in terms of API, still valid for understanding registration. In the mean time check the latest handlers/editors if you are implementing a new plugin.__

This document covers everything needed to implement a **handler** (binary
format interpreter) or **editor** (UI widget), and how they interact through
the session and dispatcher pipeline. In general payloads of type `bytes` are handled automatically by the contracts, for more complex plugins you will need to override to the desired type.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Handler API](#handler-api)
   - [Registration](#handler-registration)
   - [BaseHandler](#basehandler)
   - [PhysicalHandler](#physicalhandler)
   - [ContainerHandler](#containerhandler)
   - [LeafHandler](#leafhandler)
   - [RebuildResult](#rebuildresult)
3. [Editor API](#editor-api)
   - [Registration](#editor-registration)
   - [BaseEditor: Minimum implementation](#baseeditor--minimum-implementation)
   - [Loading lifecycle](#loading-lifecycle)
   - [Save lifecycle](#save-lifecycle)
   - [Undo / Redo](#undo--redo)
   - [BaseViewer](#baseviewer)
4. [Thread model](#thread-model)
5. [Complete examples](#complete-examples)

---

## Architecture Overview

```
------------------------------ OPENING AN EDITOR ------------------------------

  [ MAIN THREAD (UI) ]                  [ WORKER THREAD (Background) ]

User opens node
 ├─ dispatcher begins load ────────────────────┐
 │                                             ▼
 │                                    handler.prepare_editor_data(raw_bytes)
 │                                    (Decodes audio, unswizzles textures)
 │                                             │
 │ ┌── finished signal (payload) ──────────────┘
 ▼ ▼
EditorSession.complete(payload)
 ├─ editor.receive_data(payload)
 └─ editor._populate_ui(payload)


-------------------------------- SAVING EDITS --------------------------------

  [ MAIN THREAD (UI) ]                   [ WORKER THREAD (Background) ]

User clicks Save
 ├─ editor.snapshot() -> freeze current data
 └─ dispatcher.apply_edit(node, data)
      │
      ├─► [If data is `bytes`]
      │    ├─ tracker.mark_modified()
      │    └─ session.confirm_save()
      │
      └─► [If data is non-bytes] ──────────────┐
                                               ▼
                                      handler.decode_editor_data(data)
                                      (Repacks payload back to raw binary)
                                               │
   ┌── success / fail signal ──────────────────┘
   ▼
session.confirm_save() (on success)
session.reject_save()  (on failure)
```

Handlers run on **worker threads**. Editors run on the **main thread**.
They never call each other directly. The Dispatcher and the EditorSession drive the states.

---

## Handler API

### Handler Registration

```python
from core.contracts  import ContainerHandler, RebuildResult
from core.registry   import Registry
from core.workers    import ActionDef, ActionType

@Registry.register(
    name='My Format Handler',
    extensions=('.mfmt',),
    supported_actions=(
        ActionDef('Unpack',     ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG),
        ActionDef('Export',     ActionType.EXPORT),
    ),
)
class MyFormatHandler(ContainerHandler):
    ...
```

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Human-readable name, shown in the UI and logs |
| `extensions` | `tuple[str, ...]` | File extensions claimed (ex. `('.kods',)`) |
| `supported_actions` | `tuple[ActionDef, ...]` | Context menu entries |
| `is_fallback` | `bool` | If `True`, supplies all actions for all nodes |

**ActionDef routing:**

| `ActionType` | What happens |
|---|---|
| `TREE_EXPAND` | Calls `get_file_tree()`, result inserted into VFS |
| `DIALOG` | Calls `execute_action()`, result shown in a popup |
| `EXPORT` | Calls `execute_action()`, result written to a file |
| `IMPORT` | Calls `execute_action()` with a source path |
| `PROCESS` | Calls `execute_action()`, result returned raw (ex. for an editor) |

---

### BaseHandler

Abstract base class. Never use `BaseHandler` directly. Instead choose the specialized subclass that matches your use case:

- Building an asset editor/viewer? [LeafHandler](#leafhandler)
- Unpacking an archive or expanding the file tree? [ContainerHandler](#containerhandler).
- Handling whole ISOs? [PhysicalHandler](#physicalhandler)

#### `get_file_tree() → VfsNode`  *(abstract)*

Build and return the file tree. Called on a **worker thread**
when a `TREE_EXPAND` action fires.

```python
def get_file_tree(self) -> VfsNode:
    root = VfsNode(name='My Archive', parent=self.parent_node)
    for entry in self._parse_entries():
        node = VfsNode(
            name=entry.name, offset=entry.offset,
            size=entry.size, parent=root,
        )
        root.append_child(node)
    return root
```

---

#### `get_raw_node(node: VfsNode) → bytes`  *(abstract)*

Return raw bytes for a single node without any pending edits.
Called by the navigator when resolving a node's content.

```python
def get_raw_node(self, node: VfsNode) -> bytes:
    return bytes(self.payload_view[node.offset: node.offset + node.size])
```

---

#### `rebuild_node(node, staged_nodes) → bytes | RebuildResult`  *(abstract)*

Repack modified children into the container. For each child use
`child.pending_data` if staged, otherwise `get_raw_node(child)`.
The handler instance is injected with a `self.task_handle` attribute post-instantiation.
Progress tracking and task interrupting are handled automatically by the dispatcher.
If you require manual interrupting during tight loops you can call
`self.task_handle.checkpoint()`

```python
def rebuild_node(self, node, staged_nodes):
    staged = set(staged_nodes)
    out = BytesIO()
    for child in node.children:
        data = (
            child.pending_data
            if child in staged and child.pending_data
            else self.get_raw_node(child)
        )
        out.write(data)
        if self.task_handle:
            self.task_handle.checkpoint()
    return out.getvalue()
```

---

#### RebuildResult

Return `RebuildResult(payload, target_data)` when the rebuild also
produces new bytes for a dependent node (ex. datacenter headers).

```python
class RebuildResult(NamedTuple):
    payload:     bytes
    target_data: bytes | None = None
```

`payload`: new bytes for the node being rebuilt.
`target_data`: new bytes for `node.target`. The navigator writes these
to `target_node.pending_data` and adds it to the rebuild queue.


---

#### `prepare_editor_data(node, raw_bytes) → Any`

Pre-process bytes before the editor sees them. Override for expensive
work (audio decode, image unswizzle) that would otherwise freeze the main thread.

Default implementation returns `raw_bytes` unchanged.

```python
def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> MyPayload:
    image = unswizzle(raw_bytes)
    return MyPayload(image=image, raw_bytes=raw_bytes)
```

The return value is whatever type `editor.receive_data` expects. Handler and editor share an implicit contract on this type.

---

#### `decode_editor_data(node, payload) → bytes`

Convert the editor's payload back into raw bytes for `tracker.mark_modified`.
Only needed when `prepare_editor_data` returns a non-bytes type.

```python
def decode_editor_data(self, node: VfsNode, payload: MyPayload) -> bytes:
    return reswizzle(payload.image)
```

---

#### `execute_action(node, action_name, **kwargs) → Any`

Routes context-menu actions registered in `supported_actions`.

```python
def execute_action(self, node, action_name, **kwargs):
    if action_name == 'Properties':
        return f'Size: {node.size}  Offset: {hex(node.offset)}'
    if action_name == 'Export':
        path: Path = kwargs['file_path']
        path.write_bytes(self.get_raw_node(node))
        logger.info(f'Exported to {path.name}')
```

---

### PhysicalHandler

ONLY for ISO handling, most plugin developers can ignore this section. The init handle is opened in `__init__` and **must** be released after `get_file_tree()` via `release_handle()`. All subsequent reads must open private handles:

```python
def get_raw_node(self, node: VfsNode) -> bytes:
    with open(self.path, 'rb') as fh:
        fh.seek(node.offset)
        return fh.read(node.size)
```

`rebuild_node` for `PhysicalHandler` has a different signature. It takes `task_handle` explicitly as an argument, writes directly to disk, and returns a success `bool`:

```python
def rebuild_node(
    self, node, staged_nodes,
    output_path: Path,
    task_handle: TaskHandle,
) -> bool:
    ...
```

---

### ContainerHandler

For in-memory archives. Source is `bytes`, stored as `BytesIO` in
`self.handle`. Read the full payload in `__init__`:

```python
class MyContainerHandler(ContainerHandler):
    def __init__(self, source: bytes, parent: VfsNode) -> None:
        super().__init__(source)
        self.payload_view = memoryview(self.handle.read())
```

---

### LeafHandler

For single-file formats (audio, images). Provides no-op stubs for
`get_file_tree` and `get_raw_node`. Implement only `execute_action`
and optionally `prepare_editor_data` if linked to an editor.

```python
@Registry.register(name='TAC Audio', extensions=('.020',))
class TacHandler(LeafHandler):
    def prepare_editor_data(self, node, raw_bytes):
        wav, info = decode_tac(raw_bytes)
        return TacPayload(wav=wav, info=info, raw=raw_bytes)
```

---

## Editor API

### Editor Registration

```python
from core.contracts import BaseEditor
from core.registry  import Registry

@Registry.register_editor(
    name='My Format Editor',
    handler=MyFormatHandler,    # required
    extensions=('.mfmt',),
    is_fallback=False,
)
class MyFormatEditor(BaseEditor):
    ...
```

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Shown in context menu as "{role} in {name}" |
| `handler` | `type[BaseHandler]` | Handler whose `prepare_editor_data` feeds this editor |
| `extensions` | `tuple[str, ...]` | Extensions this editor claims |
| `is_fallback` | `bool` | Offered for any file regardless of extension |

---

### BaseEditor: minimum implementation

**Mutable editor**, two methods required:

```python
class MyEditor(BaseEditor):

    def _populate_ui(self, data: Any) -> None:
        '''Render the payload. Called on receive_data and on discard_changes.'''
        ...

    def current_data(self) -> Any:
        '''Return live widget state for saving.
        Only needed when your editor modifies the data.'''
        return ...
```

**Read-only editor**, inherit from `BaseViewer`, one method required:

```python
class MyViewer(BaseViewer):

    def _populate_ui(self, data: Any) -> None:
        ...
```

---

### Signals

| Signal | Signature | Purpose |
|---|---|---|
| `dataChanged` | `bool` | Emit via `set_dirty(True/False)`. Drives Save/Revert button state. |
| `undo_state_changed` | `(bool, bool)` | `(can_undo, can_redo)`. Shows/enables Undo/Redo buttons. |

Both are declared on `BaseEditor`.

---

### Class attributes

| Attribute | Default | Description |
|---|---|---|
| `is_mutable` | `True` | Set `False`, via `BaseViewer`, to hide Save/Revert |

---

### Loading lifecycle

Called in this exact order for every editor open.

#### `begin_loading(node: VfsNode) → None`

Called on the **main thread** before the worker starts. Show a placeholder.
`super().begin_loading(node)` stores `self.current_node`.

```python
def begin_loading(self, node: VfsNode) -> None:
    super().begin_loading(node)
    self._label.setText(f'Loading {node.name}...')
    self._set_controls_enabled(False)
```

#### `receive_data(result: Any, data_resolver: Callable | None) → None`

Called on the **main thread** when the worker finishes. Default implementation
handles `result: bytes` only. Calls `_populate_ui(result)` and stores
`result` in `_original_payload`.

Override for non-bytes payloads:

```python
def receive_data(self, result: Any, data_resolver=None) -> None:
    self._data_resolver   = data_resolver
    self._original_payload = result

    if not isinstance(result, MyPayload):
        self.show_error(f'Expected MyPayload, got {type(result).__name__}')
        return

    self.my_data = result
    self._populate_ui(result)
```

#### `_populate_ui(data: Any) → None`  *(abstract)*

Populate the widget. `data` is whatever `handler.prepare_editor_data`
returned (or `_original_payload` when called from `discard_changes` for reverting the changes).

For non-bytes editors this method must handle **both** initial load and
revert.

```python
def _populate_ui(self, data: Any) -> None:
    if isinstance(data, MyPayload):
        self.img     = data.image.copy()
        self.raw     = data.raw_bytes
    self._render()
```

#### `show_error(message: str) → None`

Called when the worker raises. Default logs to `logger.error`. Override
for inline UI feedback:

```python
def show_error(self, message: str) -> None:
    self._label.setText(f'Error: {message}')
    self._set_controls_enabled(False)
    super().show_error(message)    # also logs
```

#### `cleanup() → None`

Called when the editor is closed. Release timers, sinks, open handles.
Always call `super().cleanup()` last:

```python
def cleanup(self) -> None:
    self._timer.stop()
    self._audio_sink.stop()
    self.my_data = None
    super().cleanup()
```

---

### Save lifecycle

EditorPage drives saving. Editors do not initiate saves they signal for them.

#### `current_data() → Any`

Return the live state of your editor. Called by `snapshot()` before
every save. Override when your editor holds non-bytes state:

```python
def current_data(self) -> Any:
    return (self.img, self.raw) if self.img else self._original_payload
```

Default returns `self._original_payload` (correct for bytes-only editors ex. HexEditor).

#### `confirm_changes_applied() → None`

Called when save succeeded. The base implementation advances
`_original_payload` to `_pending_data` and calls `set_dirty(False)`.

Override to also reset your history state so the asterisk clears:

```python
def confirm_changes_applied(self) -> None:
    self.history.reset_to_clean()       # mark current state as the new saved baseline
    super().confirm_changes_applied()   # advances _original_payload, calls set_dirty(False)
```

What "reset to clean" means depends on your history implementation. In general, you must clear the undo/redo stacks and re-initialise from the current state so prior edits can no
longer be undone back past the save point.

#### `reject_changes_applied(reason: str) → None`

Called when save failed. Base implementation clears `_pending_data` and
logs the error. Override for custom UI feedback.

#### `set_dirty(state: bool) → None`

Call this whenever your content changes. Emits `dataChanged(state)`,
which drives the Save/Revert button state in EditorPage:

```python
def _on_cell_edited(self):
    self.set_dirty(True)
```

#### `discard_changes() → None`

Called when the user clicks Revert. Base implementation calls
`_populate_ui(self._original_payload)` and `set_dirty(False)`.

Override only when you need extra teardown, such as when clearing a custom history stack:

```python
def discard_changes(self) -> None:
    if not self.is_dirty() or not self.current_node:
        return
    self._pending_data = None
    self._populate_ui(self._original_payload)    # restores original state
    self.set_dirty(False)                        # always call explicitly after revert
```

---

### Undo / Redo

Implement a history manager (custom or `QUndoStack`) and wire it to
`undo_state_changed`. EditorPage shows the Undo/Redo buttons the first time it fires and
hides them when both are `False`.

The dirty state must always be driven by **explicit `set_dirty()` calls**.
Call `set_dirty(True)` when a change is recorded, and`set_dirty(False)` after a successful
save or a full revert. For more complete examples of custom and QUndoStack implementations see HexEditor and FisEditor respectively.

```python
class MyEditor(BaseEditor):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = MyHistoryManager()
        self.history.state_changed.connect(self._on_history_changed)

    def _on_history_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.undo_state_changed.emit(can_undo, can_redo)

    def undo(self) -> None:
        self.history.undo()
        self._render()
        self.set_dirty(self.history.has_changes())

    def redo(self) -> None:
        self.history.redo()
        self._render()
        self.set_dirty(self.history.has_changes())

    def confirm_changes_applied(self) -> None:
        self.history.reset_to_clean()   # clear undo/redo
        super().confirm_changes_applied()   # advances _original_payload, set_dirty(False)

    def discard_changes(self) -> None:
        if not self.is_dirty() or not self.current_node:
            return
        self._pending_data = None
        self._populate_ui(self._original_payload)   # reinitialises history stack
        self.set_dirty(False)
```

`undo_state_changed` is only emitted when the history state changes.

---

### BaseViewer

Convenience subclass for immutable plugins.
`BaseViewer(BaseEditor)` with `is_mutable = False`.

`set_dirty` is a no-op so `dataChanged` is never emitted. Save,
Revert, Undo, and Redo are hidden by EditorPage automatically.

```python
@Registry.register_editor(
    name='TAC Audio Viewer', handler=TacHandler,
    extensions=('.020',)
)
class TacAudioEditor(BaseViewer):

    def begin_loading(self, node):
        super().begin_loading(node)
        self._label.setText(f'Loading {node.name}...')

    def receive_data(self, result, data_resolver=None):
        if not isinstance(result, TacPayload):
            self.show_error(f'Expected TacPayload, got {type(result).__name__}')
            return
        self._original_payload = result
        self._populate_ui(result)

    def _populate_ui(self, data):
        if isinstance(data, TacPayload):
            self._load_audio(data.wav_bytes, data.info)

    def show_error(self, message):
        self._label.setText(f'Error: {message}')
```

---

## Thread model

| Location | Thread | Safe Qt calls |
|---|---|---|
| `prepare_editor_data` | Worker | No |
| `decode_editor_data` | Worker | No |
| `execute_action` | Worker | No |
| `rebuild_node` | Worker | No |
| `get_file_tree` | Worker | No |
| `begin_loading` | Main | Yes |
| `receive_data` | Main | Yes |
| `_populate_ui` | Main | Yes |
| `confirm/reject_changes_applied` | Main | Yes |
| `cleanup` | Main | Yes |

---

## Complete examples

### Minimal bytes editor

```python
from PyQt6.QtWidgets import QPlainTextEdit, QVBoxLayout
from core.contracts  import BaseEditor
from core.registry   import Registry
from core.handlers.generic_binary_leaf import GenericBinaryHandler

@Registry.register_editor(
    name='Text Editor', handler=GenericBinaryHandler,
    extensions=('.txt',)
)
class TextEditor(BaseEditor):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._edit = QPlainTextEdit(self)
        self._edit.textChanged.connect(lambda: self.set_dirty(True))
        QVBoxLayout(self).addWidget(self._edit)

    def begin_loading(self, node):
        super().begin_loading(node)
        self._edit.setPlaceholderText(f'Loading {node.name}...')
        self._edit.setEnabled(False)

    def _populate_ui(self, data):
        if isinstance(data, bytes):
            self._edit.setPlainText(data.decode('utf-8', errors='replace'))
        self._edit.setEnabled(True)

    def current_data(self) -> bytes:
        return self._edit.toPlainText().encode('utf-8')

    def show_error(self, message):
        self._edit.setPlainText(f'Error: {message}')
        super().show_error(message)

    def cleanup(self):
        self._edit.clear()
        super().cleanup()
```

### Minimal structured viewer

```python
from PyQt6.QtWidgets import QLabel, QVBoxLayout
from core.contracts  import BaseViewer
from core.registry   import Registry
from typing          import Any

@Registry.register_editor(
    name='FIS Info Viewer', handler=FisHandler,
    extensions=('.fis',)
)
class FisInfoViewer(BaseViewer):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label = QLabel('No file loaded', self)
        self._label.setWordWrap(True)
        QVBoxLayout(self).addWidget(self._label)

    def receive_data(self, result: Any, data_resolver=None) -> None:
        if not isinstance(result, FisEditorPayload):
            self.show_error(f'Expected FisEditorPayload, got {type(result).__name__}')
            return
        self._original_payload = result
        self._populate_ui(result)

    def _populate_ui(self, data: Any) -> None:
        if isinstance(data, FisEditorPayload):
            info = data.info
            self._label.setText(
                f'{info.width}×{info.height}  {info.psm_name}  {info.bpp}bpp'
            )

    def show_error(self, message: str) -> None:
        self._label.setText(f'Error: {message}')
        super().show_error(message)
```

### Structured mutable editor with undo

```python
from PyQt6.QtWidgets import QVBoxLayout, QLabel
from core.contracts  import BaseEditor
from core.registry   import Registry
from typing          import Any

@Registry.register_editor(
    name='My Structured Editor', handler=MyHandler,
    extensions=('.mfmt',)
)
class MyStructuredEditor(BaseEditor):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = MyHistoryManager()
        self.history.state_changed.connect(self._on_history_changed)
        QVBoxLayout(self).addWidget(QLabel('My editor', self))

    def _on_history_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.undo_state_changed.emit(can_undo, can_redo)

    def begin_loading(self, node) -> None:
        super().begin_loading(node)

    def receive_data(self, result: Any, data_resolver=None) -> None:
        if not isinstance(result, MyPayload):
            self.show_error(f'Expected MyPayload, got {type(result).__name__}')
            return
        self._data_resolver    = data_resolver
        self._original_payload = result
        self.my_state          = result.data.copy()
        self.set_dirty(False)
        self._populate_ui(result)

    def _populate_ui(self, data: Any) -> None:
        if isinstance(data, MyPayload):
            self.my_state = data.data.copy()
        self.history.initialise(self.my_state)   # clears history to current state
        self._render()

    def current_data(self) -> MyPayload:
        return MyPayload(data=self.my_state)

    def undo(self) -> None:
        self.my_state = self.history.undo()
        self._render()
        self.set_dirty(self.history.has_changes())

    def redo(self) -> None:
        self.my_state = self.history.redo()
        self._render()
        self.set_dirty(self.history.has_changes())

    def confirm_changes_applied(self) -> None:
        self.history.reset_to_clean()   # clear undo/redo
        super().confirm_changes_applied()

    def discard_changes(self) -> None:
        if not self.is_dirty() or not self.current_node:
            return
        self._pending_data = None
        self._populate_ui(self._original_payload)   # reinitialises history
        self.set_dirty(False)

    def show_error(self, message: str) -> None:
        super().show_error(message)

    def cleanup(self) -> None:
        self.history.clear()
        super().cleanup()

    def _render(self) -> None:
        pass
```
