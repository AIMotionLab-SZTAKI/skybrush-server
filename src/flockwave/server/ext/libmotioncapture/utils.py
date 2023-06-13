"""Utility functions that do not fit elsewhere."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterator, Optional


@contextmanager
def extracted_driver_script() -> Iterator[Path]:
    """Context manager that extracts the separate libmotioncapture driver
    script into a temporary path when the context is entered and removes it
    when the context is exited.
    """
    from importlib.resources import read_binary

    script = read_binary(__package__, "driver.py")
    path: Optional[Path] = None

    try:
        with NamedTemporaryFile(suffix=".py", delete=False) as fp:
            fp.write(script)
            path = Path(fp.name)

        yield path
    finally:
        try:
            if path:
                path.unlink(missing_ok=True)
        except Exception as ex:
            # this is okay
            pass
