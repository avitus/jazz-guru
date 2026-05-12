"""Tool-RPC: let agent-authored Python scripts in ``python_exec`` call back
into the host registry without round-tripping through the LLM.

Hermes calls this "execute_code"; we keep the name ``python_exec`` and just
plumb a Unix domain socket per call. The script can do:

```python
for chord in chords:
    result = tools.render_midi(midi_path=chord["path"], engine="fluidsynth")
    ...
```

and each call resolves through ``registry.invoke`` with the same policy +
event emission as a direct LLM-driven tool call.
"""
from __future__ import annotations

from jazz_guru.actions.rpc.server import (
    DEFAULT_RPC_CALL_CAP,
    RPC_PRELUDE_TEMPLATE,
    ToolRPCServer,
    build_rpc_prelude,
)

__all__ = [
    "DEFAULT_RPC_CALL_CAP",
    "RPC_PRELUDE_TEMPLATE",
    "ToolRPCServer",
    "build_rpc_prelude",
]
