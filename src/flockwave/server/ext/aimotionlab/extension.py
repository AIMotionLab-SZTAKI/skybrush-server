import struct
import math

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
from aiocflib.crazyflie.log import LogMessage, LogSession
from .frame_handler import AiMotionMocapFrameHandler
from .drone_handler import DroneHandler
from typing import Dict, Callable, Union, Tuple, Any, List, Optional
from flockwave.server.ext.show.clock import ShowClock
from flockwave.server.tasks import wait_until
from copy import deepcopy
from errno import ENODATA
from struct import Struct
from dataclasses import dataclass

__all__ = ("aimotionlab", )


@dataclass
class TcpPort:
    """Convenience class to package all data relating to a TCP port maintained by the server. A port has a number,
    an informal name (corresponding to the same in skybrushd.jsonc), a function that gets called when a client
    connects to it, and a list of the streams connected to it."""
    port: int
    name: str
    func: Callable
    streams: List[trio.SocketStream]


class aimotionlab(Extension):
    """Extension that encapsulates all extra functions required by aimotionlab."""
    drone_handlers: List[DroneHandler]
    car_start: Optional[trio.Event]

    def __init__(self):
        super().__init__()
        self.drone_handlers = []
        self.configuration = None
        # In parameters, the keys are strings for crazyflies, and the values are lists for each parameter that
        # needs to be set at a certain show time for that drone. An element of this list therefore constist of
        # a show time, a parameter designator and a value, in that order. e.g.: [..[10.0, "stabilizer.controller", 1]..]
        self.parameters: Dict[str, List[List[Union[float, str, int]]]] = {}
        # In order to differentiate between drones, and between messages sent by drone handlers and the other
        # server components, we give colors to drone handlers. These colors are selected from the dictionary below,
        # meaning that if we put skybrush firmware on a new drone, we should add it to the dictionary (meaning we
        # designate a color for it.
        self.colors = {"04": "\033[92m",
                       "06": "\033[93m",
                       "07": "\033[94m",
                       "08": "\033[96m",
                       "09": "\033[95m"}

        # For each port, we have a function, and a list of streams connected to that port
        self.ports: Dict[int, TcpPort] = {}
        self.get_show_clock: Optional[Callable] = None
        # Using the signals extension, we subscribe to the "show:clock_changed" event, and call _on_show_clock_changed,
        # which dispatches an instance of _send_parameters_to_drone for each drone in self.parameters. If we catch
        # another of these signals, then each process instance of _send_parameters_to_drone should be cancelled, and
        # restarted with the new values in self.parameters. To achieve this, we use cancel scopes, and run the processes
        # in them. A cancel scope can then be cancelled in _on_show_clock_changed. This dictionary below stores the
        # cancel scopes, much like self.parameters stores the parameters.
        self.parameter_scopes: Dict[str, trio.CancelScope()] = {}
        self.nursery: Optional[trio.Nursery] = None
        self._log_session: Optional[LogSession] = None
        self.cf_log: Optional[Any] = None
        self.log_event: Optional[trio.Event] = None

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

    def _set_port_func(self, port_name: str, func: Callable):
        """If the port is already present in self.ports, it changes its function while maintaining its streams. If the
        port is not yet present, it adds it, with the list of streams being []"""
        tcp_port_dict = self.configuration.get("tcp_ports", {})
        try:
            port_num = tcp_port_dict[port_name]
            if port_num in self.ports:  # means we need to save its streams
                streams: List[trio.SocketStream] = self.ports[port_num].streams
                self.ports[port_num] = TcpPort(port=port_num, name=port_name, func=func, streams=streams)
            else:
                self.ports[port_num] = TcpPort(port=port_num, name=port_name, func=func, streams=[])
        except KeyError:
            self.log.warning(f"No such port in configuration: {port_name}")

    def configure(self, configuration: Configuration) -> None:
        super().configure(configuration)
        assert self.app is not None
        self.configuration = configuration
        self._set_port_func("drone", self.establish_drone_handler)
        self._set_port_func("car", self._broadcast)
        self._set_port_func("sim", self._broadcast)
        self._set_port_func("lqr", self.stream_lqr_to_cf)

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

        self.get_show_clock = self.app.import_api("show").get_clock
        signals = self.app.import_api("signals")
        broadcast = self.app.import_api("crazyflie").broadcast
        self.log.info("The new extension is now running.")
        await sleep(1.0)
        with ExitStack() as stack:
            # create a dedicated mocap frame handler
            frame_handler = AiMotionMocapFrameHandler(broadcast, configuration.get("cf_crtp_port", 1), configuration.get("channel"))
            # subscribe to the motion capture frame signal
            async with trio.open_nursery() as nursery:
                self.nursery = nursery
                stack.enter_context(
                    signals.use(
                        {
                            "motion_capture:frame": partial(
                                self._on_motion_capture_frame_received,
                                handler=frame_handler
                            ),
                            "show:upload": self._on_show_upload,
                            "show:start": self._on_show_start,
                            "show:clock_changed": self._on_show_clock_changed
                        }
                    )
                )
                for port_num, tcp_port in self.ports.items():
                    handler = partial(tcp_port.func, port=port_num)
                    nursery.start_soon(partial(trio.serve_tcp, handler=handler, port=port_num, handler_nursery=nursery))
            self.nursery = None

    def _set_cf_log(self, message: LogMessage):
        """Handler for the logging session. Saves the log message and sets the event indicating this."""
        if self.log_event is not None:
            # self.log.info(f"[{trio.current_time():.2f}]message.items: {message.items}, type {type(message.items[0])}")
            self.cf_log = message.items
            self.log_event.set()

    async def stream_lqr_to_cf(self, stream: trio.SocketStream, port: int):
        """ function we use to continously stream LQR parameters to the drone"""
        self.log.info(f"Client connected to LQR port.")
        cf_id: bytes = await stream.receive_some()
        cf_id: str = cf_id.decode()
        frequency = 15
        timeout = 1/frequency
        tcp_format = "<BBI" + "f" * 66  # 48 for cf
        log_str = "Lqr2.traj_timestamp"  # "Lqr2.traj_timestamp" for bumblebee
        try:
            uav: CrazyflieUAV = self.app.object_registry.find_by_id(cf_id)
            cf = uav._get_crazyflie()
            self.log.info(f"cf{cf_id} found.")
            self.ports[port].streams.append(stream)
            # we need to use the drone's logger, but a separate log session
            self.log_event = trio.Event()
            self.cf_log = (0,)
            self._log_session = cf.log.create_session()
            self._log_session.configure(graceful_cleanup=True)
            self._log_session.create_block(
                log_str,
                frequency=frequency,
                handler=self._set_cf_log,
            )
            async with self._log_session:
                self.nursery.start_soon(self._log_session.process_messages)
                # # FOR TESTING:
                # await sleep(2)
                # await stream.send_all(b'START')
                while True:
                    await self.log_event.wait()
                    self.log_event = trio.Event()  # refresh
                    t = self.cf_log[0]
                    t_bytes = struct.pack('I', t)  # unsigned int
                    try:  # tell the client what time it is according to drone
                        with trio.move_on_after(timeout):
                            await stream.send_all(t_bytes)
                    except trio.BrokenResourceError:
                        break
                    except Exception as e:
                        self.log.warning(e.__repr__())
                        break
                    try:
                        msg = None
                        with trio.move_on_after(timeout):
                            msg = await stream.receive_some(struct.calcsize(tcp_format))
                            # self.log.info(f"[{self.get_show_clock().seconds:.3f}]: got params")
                        if msg is not None:
                            raw_data = struct.unpack(tcp_format, msg)
                            crtp_port, channel, t_ms = raw_data[:3]
                            params = list(raw_data[3:])
                            for idx in range(math.ceil(len(params)/6)):
                                params_slice = params[idx*6: (idx+1)*6]
                                crtp_format = "<BIffffff"
                                data = [idx, t_ms] + params_slice
                                packet = Struct(crtp_format).pack(*data)
                                timed_out = True
                                with trio.move_on_after(timeout):
                                    await cf.send_packet(port=crtp_port, channel=channel, data=packet)
                                    timed_out = False
                            #     if timed_out:
                            #         self.log.warning(f"[{self.get_show_clock().seconds:.3f}]: idx {data[0]} "
                            #                          f"@{data[1]} timed out.")
                            #     else:
                            #         self.log.info(
                            #             f"[{self.get_show_clock().seconds:.3f}]: sent idx {data[0]} @{data[1]} to drone")
                            # self.log.info(f"[{self.get_show_clock().seconds:.3f}]: succesfully sent all params")
                            await sleep(5/1000)
                    except trio.BrokenResourceError:
                        break
                    except Exception as e:
                        self.log.warning(e.__repr__())
                        break
                self.ports[port].streams.remove(stream)
        except KeyError:
            self.log.warning(f"UAV by ID {cf_id} is not found in the client registry.")
        except RuntimeError as e:
            self.log.warning(f"{e!r}")
        self.log.info(f"[{self.get_show_clock().seconds:.2f}]: Client disconnected from LQR port.")

    async def _broadcast(self, stream: trio.SocketStream, port: int):
        streams = self.ports[port].streams
        streams.append(stream)
        self.log.info(f"Number of connections on port {port} changed to {len(streams)}")
        while True:
            try:
                data = await stream.receive_some()
                if data:
                    for target_stream in [other_stream for other_stream in streams if other_stream != stream]:
                        await target_stream.send_all(data)
                else:
                    break
            except trio.BrokenResourceError:
                break
        streams.remove(stream)
        self.log.info(f"Number of connections on port {port} changed to {len(streams)}")

    async def _send_parameters_to_drone(self, uav: CrazyflieUAV, parameters):
        """This function dispatches the parameters listed in the self.parameters dictionary to the uav in the dictionary
        whose key is taken here as a parameter."""
        # TODO: cancel this process if show is over or drone landed etc.
        parameters = sorted(parameters, key=lambda x: x[0])  # arrange parameters by time
        assert self.get_show_clock is not None
        clock: ShowClock = self.get_show_clock()  # grab the show clock: we send parameters out as per this clock
        assert clock is not None
        with self.parameter_scopes[uav.id]:
            for t, parameter, value in parameters:
                # edge_triggered=False -> level triggered, meaning that if the clock is over the given time, the wait
                # returns immediately, i.e. we can set a time such as -1000 and it will execute at once
                await wait_until(clock, seconds=t, edge_triggered=False)
                try:  # maybe for parameters after 0, they must only be sent if show is on?
                    self.log.info(f"SHOW {clock.seconds:.2f}: setting drone {uav.id} {parameter}: {value} at t:{t:.2f}")
                    await uav.set_parameter(parameter, value)
                    # after setting the parameter, read it back, and if the difference is big, the set wasn't done!
                    read_param = await uav.get_parameter(parameter, fetch=True)
                    # self.log.info(f"drone {uav.id} {parameter}: {read_param}")
                    if abs(read_param - value) > 0.0001:
                        self.log.warning(f"PARAMETER FOR DRONE {uav.id} WASN'T SET CORRECTLY")
                        raise RuntimeError
                except RuntimeError:
                    self.log.warning(f"failed to set param {parameter} for drone {uav.id}")

    def _on_show_clock_changed(self, sender):
        """This function gets called when the "show:clock_changed" event is called."""
        self.log.info(f"Show clock changed")
        # self.parameter_scopes is a dictionary, keys are cf ids, values are Trio.CancelScope
        for parameter_scope in self.parameter_scopes.values():
            parameter_scope.cancel()  # cancel each instance of _send_parameters_to_drone
        self.parameter_scopes = {}  # clear the parameter scopes
        for uav_id in self.parameters.keys():  # for the drones that require parameter switches:
            self.parameter_scopes[uav_id] = trio.CancelScope()  # make a cancelscope for each
        for uav_id, parameters in self.parameters.items():
            if parameters is not None:
                try:
                    uav: CrazyflieUAV = self.app.object_registry.find_by_id(uav_id)
                    # Note: the parameter scopes aren't given as parameters to the function, but they could be :)
                    self.nursery.start_soon(self._send_parameters_to_drone, uav, parameters)
                except KeyError:
                    self.log.warning(f"UAV by ID {uav_id} is not found in the client registry.")

    def _on_show_upload(self, sender, parameters):
        # Note: this gets called once for each drone
        self.parameters[parameters[0]] = parameters[1]
        if parameters[1] is not None:
            self.log.warning(f"Parameter updates detected for drone {parameters[0]}.")

    def _on_show_start(self, sender):
        self.log.info(f"Starting show!")
        for stream in self.ports[self.configuration.get("tcp_ports", {})["car"]].streams:
            self.nursery.start_soon(stream.send_all, b'6')

        for stream in self.ports[self.configuration.get("tcp_ports", {})["sim"]].streams:
            self.nursery.start_soon(stream.send_all, b'00_CMDSTART_show_EOF')

        for stream in self.ports[self.configuration.get("tcp_ports", {})["lqr"]].streams:
            self.nursery.start_soon(stream.send_all, b'START')

    def _on_motion_capture_frame_received(
            self,
            sender,
            *,
            frame: "MotionCaptureFrame",
            handler: AiMotionMocapFrameHandler
    ) -> None:
        crazyflies: List[Crazyflie] = self._crazyflies()
        self.nursery.start_soon(handler.notify_frame, frame, crazyflies)

    async def establish_drone_handler(self, drone_stream: trio.SocketStream, port: int):
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
