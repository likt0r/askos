"""A command's declaration: its handler, arguments, access and Activity behavior."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Declaration:
    handler: object
    usage: str
    options: dict = field(default_factory=dict)
    needs_root: bool = True
    control_room: bool = False
    read_only: bool = False
    own_entries: bool = False
    kind: str = "action"
    describe: object = None
    order: int = 0
    option_exit: int = 2
    prefix: object = ""
    positional: bool = True
    allow_unknown: bool = False
    option_offset: int = 0


def declare(handler, usage, *, options=None, needs_root=True, control_room=False, read_only=False,
            own_entries=False, kind="action", describe=None, order=0, option_exit=2, prefix="", positional=True,
            allow_unknown=False, option_offset=0):
    """Keep all command facts beside its handler; usage is also the operator reference."""
    return Declaration(handler, usage, options or {}, needs_root, control_room, read_only, own_entries,
                       kind, describe, order, option_exit, prefix, positional, allow_unknown, option_offset)
