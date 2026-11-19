"""A command's declaration: its handler, arguments, access and Activity behavior."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Declaration:
    handler: object
    usage: str
    options: dict = field(default_factory=dict)
    needs_root: bool = True
    control_room: bool = False
    # what the Control Room may pass beyond the command's VM or other words: the options it may
    # give (None: every declared one) and how many positional arguments at most (None: any)
    control_room_options: tuple = None
    control_room_positional: int = None
    read_only: bool = False
    own_entries: bool = False
    kind: str = "action"
    describe: object = None
    option_exit: int = 2
    prefix: object = ""
    positional: bool = True
    allow_unknown: bool = False
    option_offset: int = 0
    # no message repeats an unknown option or a value (a secret's commands)
    hide_arguments: bool = False


def declare(handler, usage, *, options=None, needs_root=True, control_room=False, control_room_options=None,
            control_room_positional=None, read_only=False, own_entries=False, kind="action", describe=None,
            option_exit=2, prefix="", positional=True, allow_unknown=False, option_offset=0,
            hide_arguments=False):
    """Keep all command facts beside its handler; usage is also the operator reference."""
    return Declaration(handler, usage, options or {}, needs_root, control_room, control_room_options,
                       control_room_positional, read_only, own_entries, kind, describe, option_exit, prefix,
                       positional, allow_unknown, option_offset, hide_arguments)
