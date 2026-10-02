"""A local process lease prevents duplicate paid research scans of one database."""
from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import BinaryIO


class ResearchLease:
    def __init__(self, database: Path) -> None:
        database = database.expanduser().resolve()
        path = database.with_name(database.name + '.research.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: BinaryIO | None = path.open('a+b')
        try:
            if os.name == 'nt':
                msvcrt = import_module("msvcrt")
                self._file.seek(0, os.SEEK_END)
                if self._file.tell() == 0:
                    self._file.write(b'\0')
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise ValueError('research is already running in another session') from None

    def close(self) -> None:
        if self._file is not None:
            if os.name == 'nt':
                msvcrt = import_module("msvcrt")
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            self._file.close()
            self._file = None

    def __enter__(self) -> ResearchLease:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
