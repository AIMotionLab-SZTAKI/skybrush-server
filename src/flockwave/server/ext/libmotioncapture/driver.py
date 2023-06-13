"""Driver script that connects to a remote mocap system with libmotioncapture
and prints the frames in JSON format to stdout.

This script is meant to be executed in a separate process outside the context
of Skybrush, using the same Python interpreter as the one used by Skybrush
itself. The ``libmotioncapture`` extension in Skybrush takes care of
starting this script and reading its stdout in a timely manner.
"""

import json
import motioncapture
import sys

from argparse import ArgumentParser
from time import time
from typing import Any, Callable, Optional, Tuple, TypeVar


T = TypeVar("T")

encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def key_value_pair(value: str) -> Tuple[str, str]:
    """Parser function for the argument parser that takes a string in key=value
    format and returns a tuple consisting of the key and the value only. Leading
    and trailing whitespace are stripped.
    """
    key, sep, value = value.partition("=")
    return key.strip(), value.strip()


def create_parser() -> ArgumentParser:
    """Creates the argument parser for the script."""
    parser = ArgumentParser()

    parser.add_argument("type", help="type of the mocap system to connect to")
    parser.add_argument(
        "-p",
        "--param",
        type=key_value_pair,
        action="append",
        dest="parameters",
        default=[],
    )

    return parser


def send(obj: Any) -> None:
    """Sends the given object in JSON format to the standard output stream."""
    print(encoder.encode(obj))
    sys.stdout.flush()


def wrap_exceptions(func: Callable[[], T]) -> Callable[[], Optional[T]]:
    """Decorator that takes a function and converts it into another function
    that catches all exceptions from the function and logs them in JSON
    format.
    """

    def decorated() -> Optional[T]:
        try:
            return func()
        except Exception as ex:
            send({"type": "error", "error": str(ex)})

    return decorated


@wrap_exceptions
def main() -> int:
    """Main entry point of the script.

    Returns:
        error code to return to the OS
    """
    parser = create_parser()
    args = parser.parse_args()

    options = dict(args.parameters)

    try:
        if args.type == "test":
            mc = motioncapture.MotionCaptureTest(0.04, [])
        else:
            mc = motioncapture.connect(args.type, options)
    except Exception as ex:
        if "incompatible function arguments" in str(ex):
            # old libmotioncapture where 'options' had to be a string
            try:
                hostname = options.pop("hostname")
            except KeyError:
                raise RuntimeError("hostname not specified")

            if options:
                raise RuntimeError("unhandled options: " + ", ".join(options.keys()))

            mc = motioncapture.connect(args.type, hostname)
        else:
            raise

    items = []
    message = {"items": items, "t": 0}
    while True:
        mc.waitForNextFrame()
        message["t"] = time()

        items.clear()
        for name, obj in mc.rigidBodies.items():
            rot = obj.rotation
            encoded_pos = tuple(round(float(x), 3) for x in obj.position)
            encoded_rot = (rot.w, rot.x, rot.y, rot.z) if rot is not None else None
            items.append((name, encoded_pos, encoded_rot))

        if items:
            send(message)

    return 0


if __name__ == "__main__":
    sys.exit(main())
