import sys

from contextlib import ExitStack
from functools import partial
from trio import open_nursery
from typing import Any, Dict, Sequence, TYPE_CHECKING

from flockwave.connections.process import ProcessConnection
from flockwave.server.ext.base import Extension
from flockwave.server.model import ConnectionPurpose

from .channel import LibmotioncaptureConnection
from .utils import extracted_driver_script

if TYPE_CHECKING:
    from flockwave.server.app import SkybrushServer

__all__ = ("LibmotioncaptureMocapExtension",)

ConnectionSpec = Dict[str, Any]
"""Typing specification for a single item in the ``connections`` key of the
configuration.
"""


class LibmotioncaptureMocapExtension(Extension):
    async def run(self, app: "SkybrushServer", configuration):
        """This function is called when the extension was loaded.

        The signature of this function is flexible; you may use zero, one, two
        or three positional arguments after ``self``. The extension manager
        will detect the number of positional arguments and pass only the ones
        that you expect.

        Parameters:
            app: the Skybrush server application that the extension belongs to.
                Also available as ``self.app``.
            configuration: the configuration object. Also available in the
                ``configure()`` method.
            logger: Python logger object that the extension may use. Also
                available as ``self.log``.
        """
        connection_specs: Sequence[ConnectionSpec] = configuration.get(
            "connections", ()
        )

        assert self.log is not None

        with ExitStack() as stack:
            if connection_specs:
                driver_script = stack.enter_context(extracted_driver_script())
            else:
                driver_script = None

            async with open_nursery() as nursery:
                count = 0
                for index, connection_spec in enumerate(connection_specs):
                    type = connection_spec.get("type")
                    if not type:
                        self.log.error("Connection specification #{index} has no type")
                        continue

                    conn_id = f"lmc/{index}"
                    name = (
                        connection_spec.get("name")
                        or f"Mocap connection {index} ({type})"
                    )

                    args = [sys.executable, str(driver_script)]
                    for key, value in connection_spec.items():
                        if key != "type":
                            args.append("-p")
                            args.append(f"{key}={value}")
                    args.append(type)

                    connection = ProcessConnection.create_in_nursery(nursery, args)
                    stack.enter_context(
                        app.connection_registry.use(
                            connection,
                            conn_id,
                            name,
                            purpose=ConnectionPurpose.mocap,  # type: ignore
                        )
                    )

                    nursery.start_soon(
                        partial(
                            app.supervise,
                            connection,
                            task=partial(
                                self.handle_libmotioncapture_connection,
                                id=conn_id,
                                name=name,
                            ),  # type: ignore
                        )
                    )

                    count += 1

                if count > 1:
                    self.log.info(f"Using {count} libmotioncapture connections")
                elif count:
                    self.log.info(f"Using libmotioncapture connection")

    async def handle_libmotioncapture_connection(
        self, connection: ProcessConnection, id: str, name: str
    ) -> None:
        assert self.log is not None

        log = self.log

        log.info(
            f"Started libmotioncapture process for {name!r}",
            extra={"semantics": "success", "id": id},
        )

        try:
            await self._handle_libmotioncapture_connection(
                LibmotioncaptureConnection(connection)
            )
        except RuntimeError as ex:
            log.error(str(ex))
        except Exception:
            log.exception(
                f"Unexpected error while handling libmotioncapture connection {name!r}",
                extra={"id": id},
            )
        finally:
            log.info(
                f"Connection to libmotioncapture process closed for {name!r}",
                extra={"id": id},
            )
            await connection.close()

    async def _handle_libmotioncapture_connection(
        self, conn: LibmotioncaptureConnection
    ) -> None:
        assert self.app is not None
        assert self.log is not None

        # Grab the signal we need to post frames, as well as the frame factory
        # from the motion_capture extension
        create_frame = self.app.import_api("motion_capture").create_frame
        enqueue_frame = self.app.import_api("motion_capture").enqueue_frame

        # Wire the signals to the connection object
        conn.frame_factory = create_frame

        async for frame in conn.iter_frames():
            enqueue_frame(frame)
