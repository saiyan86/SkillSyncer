"""SkillSyncer — agent skills that sync, fill, and protect themselves."""

import sys

_REQUIRED_PYTHON = (3, 12)
if sys.version_info < _REQUIRED_PYTHON:
    raise RuntimeError(
        f"SkillSyncer requires Python "
        f"{_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]} or newer; "
        f"this interpreter is {sys.version_info.major}.{sys.version_info.minor}."
    )

__version__ = "0.1.0"
