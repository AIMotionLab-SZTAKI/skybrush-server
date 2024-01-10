import trio
from flockwave.ext.base import Configuration
from trio import sleep, sleep_until
from flockwave.server.app import SkybrushServer
from flockwave.server.ext.base import Extension
from flockwave.server.ext.crazyflie.driver import CrazyflieUAV
from contextlib import ExitStack
from functools import partial
from flockwave.server.ext.motion_capture import MotionCaptureFrame
from aiocflib.crazyflie import Crazyflie
from .frame_handler import AiMotionMocapFrameHandler
from .drone_handler import DroneHandler
from typing import Dict, Callable, Union, Tuple, Any, List, Optional
from copy import deepcopy
from errno import ENODATA

__all__ = ("aimotionlab", )


class aimotionlab(Extension):
    """Extension that broadcasts the pose of non-UAV objects to the Crazyflie drones."""
    drone_handlers: List[DroneHandler]
    car_start: Optional[trio.Event]

    def __init__(self):
        super().__init__()
        self.drone_handlers = []
        self.configuration = None
        self.parameters: Dict[str, List[List[Union[float, str, int]]]] = {}
        self.colors = {"04": "\033[92m",
                       "06": "\033[93m",
                       "07": "\033[94m",
                       "08": "\033[96m",
                       "09": "\033[95m"}
        self.streams: Dict[int, List[trio.SocketStream]] = {}  # a dictionary for the tcp ports where we broadcast
        self.port_features: Dict[int, Callable] = {}  # a dictionary of what functions we should use for each tcp port

    def _crazyflies(self):
        uav_ids = list(self.app.object_registry.ids_by_type(CrazyflieUAV))
        uavs: List[CrazyflieUAV] = []
        for uav_id in uav_ids:
            uavs.append(self.app.object_registry.find_by_id(uav_id))
        try:
            return [uav._get_crazyflie() for uav in uavs]
        except RuntimeError:
            pass
            # self.log.warning(f"Connection lost to crazyflie.")

    def configure(self, configuration: Configuration) -> None:
        super().configure(configuration)
        assert self.app is not None
        self.configuration = configuration
        DRONE_PORT = configuration.get("drone_port", 6000)
        self.port_features[DRONE_PORT] = self.establish_drone_handler
        CAR_PORT = DRONE_PORT+1
        self.port_features[CAR_PORT] = partial(self._broadcast, port=CAR_PORT)
        self.streams[CAR_PORT] = []
        SIM_PORT = DRONE_PORT+2
        self.port_features[SIM_PORT] = partial(self._broadcast, port=SIM_PORT)
        self.streams[SIM_PORT] = []

    async def run(self, app: "SkybrushServer", configuration, logger):
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

        signals = self.app.import_api("signals")
        broadcast = self.app.import_api("crazyflie").broadcast
        self.log.info("The new extension is now running.")
        await sleep(1.0)
        with ExitStack() as stack:
            # create a dedicated mocap frame handler
            frame_handler = AiMotionMocapFrameHandler(broadcast, configuration.get("cf_port", 1), configuration.get("channel"))
            # subscribe to the motion capture frame signal
            async with trio.open_nursery() as nursery:
                stack.enter_context(
                    signals.use(
                        {
                            "motion_capture:frame": partial(
                                self._on_motion_capture_frame_received,
                                handler=frame_handler,
                                nursery=nursery
                            ),
                            "show:upload": self._on_show_upload,
                            "show:start": partial(
                                self._on_show_start,
                                nursery=nursery),
                        }
                    )
                )
                for port, func in self.port_features.items():
                    nursery.start_soon(partial(trio.serve_tcp, handler=func, port=port, handler_nursery=nursery))

    async def _broadcast(self, stream: trio.SocketStream, *, port: int):
        self.streams[port].append(stream)
        self.log.info(f"Number of connections on port {port} changed to {len(self.streams[port])}")
        while True:
            try:
                data = await stream.receive_some()
                if data:
                    for target_stream in [other_stream for other_stream in self.streams[port] if other_stream != stream]:
                        await target_stream.send_all(data)
                else:
                    break
            except trio.BrokenResourceError:
                break
        self.streams[port].remove(stream)
        self.log.info(f"Number of connections on port {port} changed to {len(self.streams[port])}")

    def _on_show_upload(self, sender, parameters):
        # Note: this gets called once for each drone
        self.parameters[parameters[0]] = parameters[1]
        if parameters[1] is not None:
            self.log.warning(f"Parameter switches detected for drone {parameters[0]}.")

    def _on_show_start(self, sender, *, nursery):
        for uav_id, parameters in self.parameters.items():
            if parameters is not None:
                uav: CrazyflieUAV = self.app.object_registry.find_by_id(uav_id)
                nursery.start_soon(self._send_parameters, uav, parameters)
        CAR_PORT = self.configuration.get("drone_port", 6000)+1
        SIM_PORT = CAR_PORT + 1
        for stream in self.streams[CAR_PORT]:
            self.log.info("Starting car!")
            nursery.start_soon(stream.send_all, b'6')
        for stream in self.streams[SIM_PORT]:
            self.log.info("Starting Sim!")
            nursery.start_soon(stream.send_all, b'00_CMDSTART_show_EOF')

    async def _send_parameters(self, uav: CrazyflieUAV, parameters):
        start_time = trio.current_time()
        if uav.is_in_drone_show_mode:
            for t, parameter, value in parameters:
                await sleep_until(start_time + t)
                try:
                    await uav.set_parameter(parameter, value)
                    self.log.info(f"set param {parameter} to {value} for drone {uav.id}")
                except RuntimeError:
                    self.log.warning(f"failed to set param {parameter} for drone {uav.id}")

    def _on_motion_capture_frame_received(
            self,
            sender,
            *,
            frame: "MotionCaptureFrame",
            handler: AiMotionMocapFrameHandler,
            nursery: trio.Nursery
    ) -> None:
        crazyflies: List[Crazyflie] = self._crazyflies()
        nursery.start_soon(handler.notify_frame, frame, crazyflies)

    async def establish_drone_handler(self, drone_stream: trio.SocketStream):
        # when a client is trying to connect (i.e. a script wants permission to give commands to a drone),
        # we must check what uavs are recognised by the server:
        uav_ids = list(self.app.object_registry.ids_by_type(CrazyflieUAV))
        # take away the ones that already have a handler
        taken_ids = [handler.uav.id for handler in self.drone_handlers]
        # and what's left is the drones up for grabs
        available_ids = [drone_id for drone_id in uav_ids if drone_id not in taken_ids]
        if len(available_ids) != 0:  # if there are free drones, tell the user
            self.log.info(f"TCP connection made. Valid drone IDs: {uav_ids}. "
                          f"Of these the following are not yet taken: {available_ids}")
            request = await drone_stream.receive_some()
            request = request.decode('utf-8')
            # The client will send a request for a drone handler beginning with REQ_, i.e.: REQ_06
            if 'REQ_' in request:
                requested_id = request.split('REQ_')[1]
                if requested_id not in available_ids:
                    self.log.warning(f"The requested ID isn't available.")
                    await drone_stream.send_all(b'ACK_00')
                    return
            else:
                self.log.warning(f"Wrong request.")
                await drone_stream.send_all(b'ACK_00')
                return
            try:
                uav: CrazyflieUAV = self.app.object_registry.find_by_id(requested_id)
                color = self.colors[uav.id] if uav.id in self.colors else "\033[92m"
                handler = DroneHandler(uav, drone_stream, self.log, self.configuration, color=color)
                self.drone_handlers.append(handler)
                self.log.info(f"Made handler for drone {requested_id}. "
                              f"Currently we have handlers for the following drones: "
                              f"{[handler.uav.id for handler in self.drone_handlers]}")
                acknowledgement = f"ACK_{requested_id}"
                await drone_stream.send_all(acknowledgement.encode('utf-8'))
                await handler.listen()
                self.drone_handlers.remove(handler)
                self.log.info(f"Removing handler for drone {handler.uav.id}. Remaining handlers:"
                              f"{[drone_handler.uav.id for drone_handler in self.drone_handlers]}")
            except KeyError:
                self.log.warning(f"UAV by ID {requested_id} is not found in the client registry.")
                await drone_stream.send_all(b'ACK_00')
        else:  # if there aren't any drones left, tell them that
            self.log.warning(f"All drone IDs are accounted for.")
            await drone_stream.send_all(b'ACK_00')
