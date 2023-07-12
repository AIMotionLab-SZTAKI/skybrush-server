from __future__ import annotations

from typing import (
    Any,
    AsyncIterable,
    Callable,
    Iterable,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

from flockwave.channels.message import MessageChannel
# from contextlib import aclosing
from flockwave.concurrency import aclosing
from flockwave.connections import Connection
from flockwave.encoders.json import create_json_encoder
from flockwave.parsers.json import create_json_parser

if TYPE_CHECKING:
    from flockwave.server.ext.motion_capture import MotionCaptureFrame

__all__ = ("LibmotioncaptureConnection",)


class LibmotioncaptureConnection:
    """Connection to our libmotioncapture wrapper process that prints frames in
    JSON format.
    """

    _channel: MessageChannel[Any]
    """The message channel on which the JSON messages are received from the
    libmotioncapture wrapper process
    """

    frame_factory: Optional[Callable[[], "MotionCaptureFrame"]] = None
    """Function that can be called with no arguments and that returns a new
    MotionCaptureFrame_ instance. Must be set before calling ``iter_frames()``.
    """

    def __init__(self, connection: Connection):
        """Constructor."""
        parser = create_json_parser()
        encoder = create_json_encoder()

        self._channel = MessageChannel(connection, parser, encoder)

    async def iter_frames(self) -> AsyncIterable["MotionCaptureFrame"]:
        frame_factory = self.frame_factory
        assert frame_factory is not None

        async with aclosing(self._channel):
            async for message in self._channel:
                type = message.get("type", "frame")
                if type == "error":
                    raise RuntimeError(
                        str(
                            message.get(
                                "error", "unspecified error from driver process"
                            )
                        )
                    )
                elif type == "frame":
                    frame = frame_factory()
                    items: Iterable[
                        Tuple[
                            str,
                            Tuple[float, float, float],
                            Optional[Tuple[float, float, float, float]],
                        ]
                    ] = message.get("items", ())

                    for name, position, rotation in items:
                        frame.add_item(name, position, rotation)

                    yield frame
                else:
                    raise RuntimeError(
                        "unknown message type received from driver process: {type!r}"
                    )
