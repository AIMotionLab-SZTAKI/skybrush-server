import logging
import trio
from trio import sleep, sleep_until
from flockwave.server.app import SkybrushServer
from flockwave.server.ext.base import Extension
from flockwave.server.ext.crazyflie.driver import CrazyflieUAV
from flockwave.server.ext.crazyflie.trajectory import encode_trajectory, TrajectoryEncoding
from aiocflib.crazyflie.high_level_commander import TrajectoryType
from contextlib import ExitStack
from functools import partial
from flockwave.server.ext.motion_capture import MotionCaptureFrame
from aiocflib.crazyflie import Crazyflie
from trio import sleep_forever
from .handler import AiMotionMocapFrameHandler
from typing import Dict, Callable, Union, Tuple, Any, List, Optional
from flockwave.server.show.trajectory import TrajectorySpecification
from aiocflib.crazyflie.mem import MemoryHandler, MemoryHandlerBase, MemoryChannel
from aiocflib.crtp.crtpstack import MemoryType, CRTPPort
from aiocflib.utils.checksum import crc32
from aiocflib.utils.chunks import chunkify
from aiocflib.errors import error_to_string
import json
from copy import deepcopy
from errno import ENODATA

__all__ = ("aimotionlab", )


async def write(self, addr: int, data: bytes, timeout: float = 0.2, attempts: int = 3) -> None:
    """This function is what is monkey-patched to be used instead of MemoryHandlerBase.write.
    The only difference is that it can take two extra arguments: timeout and attemps, so that we are able to
    manually set these if we wish to."""
    for start, size in chunkify(
            0, len(data), step=MemoryHandler.MAX_WRITE_REQUEST_LENGTH
    ):
        status = await self._write_chunk(addr + start, data[start: (start + size)], timeout=timeout, attempts=attempts)
        if status:
            raise IOError(
                status,
                "Write request returned error code {0} ({1})".format(
                    status, error_to_string(status)
                ),
            )


async def _write_chunk(self, addr: int, data: bytes, timeout: float = 0.2, attempts: int = 3) -> int:
    """This function is what is monkey-patched to be used instead of MemoryHandlerBase._write_chunk.
        The only difference is that it can take two extra arguments: timeout and attemps, so that we are able to
        manually set these if we wish to."""
    response = await self._crazyflie.run_command(
        port=CRTPPort.MEMORY,
        channel=MemoryChannel.WRITE,
        command=self._addressing_struct.pack(self._element.index, addr),
        data=data,
        timeout=timeout,
        attempts=attempts
    )
    return response[0] if response else ENODATA
MemoryHandlerBase._write_chunk = _write_chunk
MemoryHandlerBase.write = write


async def write_with_checksum(
    handler: MemoryHandler,
    addr: int,
    data: bytes,
    *,
    only_if_changed: bool = False,
    checksum: Callable[[bytes], bytes] = crc32,
    timeout: float = 0.2,
    attempts: int = 3
) -> int:
    """See aiocflib/crazyflie/mem.py, this is the same function but with extra arguments to provide a different timeout.
    """
    expected_chksum = checksum(data)
    chksum_length = len(expected_chksum)

    if not only_if_changed:
        need_to_write = True
    else:
        observed_chksum = await handler.read(addr, chksum_length)
        need_to_write = observed_chksum != expected_chksum

    if need_to_write:
        zeros = bytes([0] * chksum_length)
        await handler.write(addr, zeros, timeout=timeout, attempts=attempts)
        await handler.write(addr + chksum_length, data, timeout=timeout, attempts=attempts)
        await handler.write(addr, expected_chksum, timeout=timeout, attempts=attempts)

    return chksum_length



# TODO: make a restart command, and use it to handle takeoff commands when the drone is exactly in 0, 0, 0
class DroneHandler:
    def __init__(self, uav: CrazyflieUAV, stream: trio.SocketStream, log: logging.Logger, configuration, color):
        self.traj = b''
        self.active_traj_ID = 2
        self.stream_data = b''
        self.transmission_active = False
        self.allow_traj_outside_show = True
        self.uav = uav
        self.stream = stream
        self.log = log
        self.hover_between = False
        self.hover_defined = False
        self.memory_partitions = configuration.get("memory_partitions")
        self.crashed = False
        self.color = color

    def print(self, text):
        reset_color = "\033[0m"
        self.log.info(f"{self.color}[drone_{self.uav.id}]: {text}{reset_color}")

    def warning(self, text):
        self.log.warning(f"[drone_{self.uav.id}]: {text}")

    async def define_hover(self):
        trajectory_data = None
        with open('./hover.json') as json_file:
            trajectory_data = json.load(json_file)
        cf = self.uav._get_crazyflie()
        try:
            trajectory_memory = await cf.mem.find(MemoryType.TRAJECTORY)
        except ValueError:
            raise RuntimeError("Trajectories are not supported on this drone") from None
        trajectory = TrajectorySpecification(trajectory_data)
        data = encode_trajectory(trajectory, encoding=TrajectoryEncoding.COMPRESSED)
        success, addr = await self.write_safely(1, trajectory_memory, data)
        if success:
            await cf.high_level_commander.define_trajectory(1, addr=addr, type=TrajectoryType.COMPRESSED)
            self.print("Defined fallback hover.")
        else:
            self.warning("Trajectory is too long.")
        self.hover_defined = True

    @staticmethod
    def get_traj_type(self, arg: bytes) -> Tuple[bool, Union[bool, None]]:
        # trajectories can either be relative or absolute. This is determined by a string/bytes, but only these two
        # values are allowed. The return tuple tells whether the argument is valid (first part) and if it's
        # relative (second part). If it wasn't valid, we just get None for the second part.
        traj_type_lower = arg.lower()  # Bit of an allowance to not be case sensitive
        # TODO: investigate whether the trajectory starts from the drone's current location
        if traj_type_lower == b'relative' or traj_type_lower == b'rel':
            return True, True
        elif traj_type_lower == b'absolute' or traj_type_lower == b'abs':
            return True, False
        else:
            return False, None

    async def write_safely(self, traj_id: int, handler, data) -> Tuple[bool, Union[int, None]]:
        # Function to upload trajectories to the drone while keeping each trajectory confined to its allotted partition.
        # Check the configuration: For a particular trajectory ID, what is the starting address of the allowed
        # partition, and what's its size? Return with info about whether the trajectory fits into the partition, and the
        # start address. This start address will be slightly different from the beginning of the partition, because the
        # data written to the partition will begin with the checksum, and only then does the trajectory data begin.
        start_addr = self.memory_partitions[traj_id]["start"]
        allowed_size = self.memory_partitions[traj_id]["size"]
        checksum_length = len(crc32(data))
        self.print(f"Checksum length: {checksum_length}, data length: {len(data)}, allowed size: {allowed_size}")
        if (len(data) + checksum_length <= allowed_size) and traj_id != self.active_traj_ID:
            try:
                checksum_length = await write_with_checksum(handler, start_addr, data, only_if_changed=True, timeout=0.8, attempts=3)
                self.print(f"Wrote trajectory to address {start_addr}")
                return True, start_addr + checksum_length
            except Exception as exc:
                self.warning(f"Couldn't write because of this exception: {exc!r}.")

        return False, None

    async def takeoff(self, arg: bytes):
        try:
            arg = float(arg)
            if arg < 0.1 or arg > 1.5:
                arg = 0.5
                self.warning(f"Takeoff height {arg}m is out of allowed range, taking off to 0.5m instead.")
            if self.uav._airborne:
                self.warning(f"Already airborne, takeoff command wasn't dispatched.")
                self.crashed = True
            else:
                await self.uav.takeoff(altitude=arg)
                self.print(f"Takeoff command dispatched.")
                await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        except ValueError:
            self.warning("Takeoff argument is not a float.")
            self.crashed = True
        except Exception as exc:
            self.warning(f"Couldn't take off because of this exception: {exc!r}. ")
            self.crashed = True

    async def land(self, arg: bytes):
        if self.uav._airborne:
            await self.uav.land()
            self.print(f"Land command dispatched.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Already on the ground, land command wasn't dispatched.")
            self.crashed = True

    async def handle_transmission(self):
        # await self.stream.send_all(b'Transmission of trajectory started.')
        self.print(f"Transmission of trajectory started.")
        start_index = self.stream_data.find(b'{')
        # If the command was 'upload', then a json file must follow. If it doesn't (we can't find the beginning b'{'),
        # then the command or the file was corrupted.
        if start_index == -1:
            self.warning("Corrupted trajectory file.")
        else:
            self.traj = self.stream_data[start_index:]
            self.transmission_active = True  # signal we're in the middle of transmission
            while not self.traj.endswith(b'_EOF'):  # receive data until we see that the file has ended
                self.traj += await self.stream.receive_some()  # append the new data to the already received data
            self.traj = self.traj[:-len(b'_EOF')]  # once finished, remove the EOF indicator
            self.transmission_active = False  # then signal that the transmission has ended
            self.print(f"Transmission of trajectory finished.")
            # await self.stream.send_all(b'Transmission of trajectory finished.')

    async def upload(self, arg: bytes):
        upload_time = trio.current_time()
        await self.handle_transmission()
        cf = self.uav._get_crazyflie()
        if self.hover_between:  # TODO: where should this be???? definitely not here
            # initiate hover while we switch trajectories so that we don't drift too far
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
        try:
            trajectory_memory = await cf.mem.find(MemoryType.TRAJECTORY)
        except ValueError:
            raise RuntimeError("Trajectories are not supported on this drone") from None
            self.crashed = True
        # print(self.traj.decode('utf-8'))
        trajectory_data = json.loads(self.traj.decode('utf-8'))
        trajectory = TrajectorySpecification(trajectory_data)
        data = encode_trajectory(trajectory, encoding=TrajectoryEncoding.COMPRESSED)
        # The trajectory IDs we upload to are 2 and 3, we swap between them like so:
        upcoming_traj_ID = 5 - self.active_traj_ID
        # Try to write the data (at this point we don't know whether it's too long or not, write_safely will tell)
        write_success, addr = await self.write_safely(upcoming_traj_ID, trajectory_memory, data)
        if write_success:
            await cf.high_level_commander.define_trajectory(
                upcoming_traj_ID, addr=addr, type=TrajectoryType.COMPRESSED)
            self.print(
                f"Defined trajectory on ID {upcoming_traj_ID} (currently active ID is {self.active_traj_ID}).")
            self.warning(f"Writing took {trio.current_time() - upload_time} sec.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Trajectory couldn't be written.")
            await self.uav.land()
            self.crashed = True

    async def start(self, arg: bytes):
        cf = self.uav._get_crazyflie()
        is_valid, is_relative = self.get_traj_type(self, arg=arg)
        upcoming_traj_ID = 5 - self.active_traj_ID
        if (self.uav.is_running_show or self.allow_traj_outside_show) and self.uav._airborne and is_valid:
            await cf.high_level_commander.start_trajectory(upcoming_traj_ID, time_scale=1, relative=is_relative,
                                                           reversed=False)
            self.print(f"Started trajectory on ID {upcoming_traj_ID}.")
            # We are now playing the trajectory with the new ID: adjust the active ID accordingly.
            self.active_traj_ID = upcoming_traj_ID
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Drone is not airborne.")
            self.crashed = True

    async def hover(self, arg: bytes):
        if not self.hover_defined:
            await self.define_hover()
        if self.uav._airborne:
            cf = self.uav._get_crazyflie()
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
            self.print(f"Hover command dispatched.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Drone is on the ground, if you want to do a takeoff, do so from Live")
            self.crashed = True

    async def command(self, cmd: bytes, arg: bytes):
        self.print(f"Command received: {cmd.decode('utf-8')}")
        # await self.stream.send_all(b'Command received: ' + cmd)
        await self.tcp_command_dict[cmd][0](self, arg)

    tcp_command_dict: Dict[
        bytes, Tuple[Callable[[Any, bytes], None], bool]] = {
        b"takeoff": (takeoff, True),  # The command takeoff expects a takeoff height as its argument
        b"land": (land, False),  # The command land takes no argument
        b"upload": (upload, True),  # The command upload takes no argument
        b"hover": (hover, False),  # The command hover takes no argument and expects no payload
        b"start": (start, True),  # The command start expects the trajectory type as its argument
    }

    def parse(self, raw_data: bytes, ) -> Tuple[Union[bytes, None], Union[bytes, None]]:
        data = raw_data.strip()
        if not data:
            return None, None
        data = data.split(b'_')
        if data[0] != b'CMDSTART':
            return b'NO_CMDSTART', None
        command = data[1]
        if command not in self.tcp_command_dict:
            return b'WRONG_CMD', None
        if self.tcp_command_dict[command][1]:  # This is a boolean signifying whether we expect an argument
            argument = data[2]
        else:
            argument = None
        return command, argument

    async def listen(self):
        while not self.crashed:
            if not self.transmission_active:
                try:
                    self.stream_data: bytes = await self.stream.receive_some()
                    if not self.stream_data:
                        break
                    cmd, arg = self.parse(self.stream_data)
                    if cmd == b'NO_CMDSTART':
                        self.print(f"Command is missing standard CMDSTART")
                        break
                    elif cmd == b'WRONG_CMD':
                        self.print(f"Command is not found in server side dictionary")
                        break
                    elif cmd is None:
                        self.warning(f"None-type command.")
                        break
                    else:
                        await self.command(cmd, arg)
                except Exception as exc:
                    self.warning(f"TCP handler crashed: {exc!r}")
                    break


class aimotionlab(Extension):
    """Extension that broadcasts the pose of non-UAV objects to the Crazyflie drones."""
    drone_handlers: List[DroneHandler]
    show_clock: [Optional]
    car_start: Optional[trio.Event]

    def __init__(self):
        super().__init__()
        self.drone_handlers = []
        self.configuration = None
        self.controller_switches: Dict[str, List[List[Union[float, int]]] ] = {}
        self.colors = {"04": "\033[92m",
                       "06": "\033[93m",
                       "07": "\033[94m",
                       "08": "\033[96m",
                       "09": "\033[95m"}
        self.car_streams: List[trio.SocketStream] = []
        self.port_features: List[Tuple[int, Callable]] = []

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
        assert self.app is not None
        self.configuration = configuration
        DRONE_PORT = configuration.get("drone_port", 6000)
        self.port_features.append((DRONE_PORT, self.establish_drone_handler))
        CAR_PORT = DRONE_PORT+1
        self.port_features.append((CAR_PORT, self.car_broadcast))

        port = configuration.get("cf_port", 1)
        channel = configuration.get("channel")
        signals = self.app.import_api("signals")
        broadcast = self.app.import_api("crazyflie").broadcast
        self.log.info("The new extension is now running.")
        self.show_clock = self.app.import_api("show").get_clock  # maybe show clock instead of sleep_until?

        await sleep(1.0)
        with ExitStack() as stack:
            # create a dedicated mocap frame handler
            frame_handler = AiMotionMocapFrameHandler(broadcast, port, channel)
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
                for port, func in self.port_features:
                    nursery.start_soon(partial(trio.serve_tcp, handler=func, port=port, handler_nursery=nursery))

    async def car_broadcast(self, stream: trio.SocketStream):
        self.car_streams.append(stream)
        self.log.info(f"Number of connections for car port changed to {len(self.car_streams)}")
        data = b''
        while not data.startswith(b'-1'):
            data = await stream.receive_some()
            if len(data) > 0:
                for target_stream in [other_stream for other_stream in self.car_streams if other_stream != stream]:
                    await target_stream.send_all(data)
        self.car_streams.remove(stream)
        self.log.info(f"Number of connections for car port changed to {len(self.car_streams)}")

    def _on_show_upload(self, sender, controllers):
        if controllers[1] is None:
            if controllers[0] in self.controller_switches:
                del self.controller_switches[controllers[0]]
                self.log.warning(f"Previously saved controller switch deleted for drone {controllers[0]}")
            else:
                pass
        else:
            self.log.warning(f"Controller switches detected for drone {controllers[0]}")
            self.controller_switches[controllers[0]] = controllers[1]

    def _on_show_start(self, sender, *, nursery):
        if len(self.controller_switches) > 0:
            start_time = trio.current_time()
            # save this so we can clear self.controller_switches and avoid 'leftover' switches from previous uploads.
            # In other words, if we want to include controller switches, we must do a fresh upload before a show start
            controller_switches = deepcopy(self.controller_switches)
            self.controller_switches = {}
            self.log.warning(f"Controller switches cleared!")
            for uav_id in controller_switches.keys():
                uav: CrazyflieUAV = self.app.object_registry.find_by_id(uav_id)
                controller_switches = [[start_time + controller_switch[0], controller_switch[1]] for controller_switch in controller_switches[uav_id]]
                nursery.start_soon(self._perform_controller_switches, uav, controller_switches)
        for stream in self.car_streams:
            self.log.info("Starting car!")
            nursery.start_soon(stream.send_all, b'6')

    async def _perform_controller_switches(self, uav: CrazyflieUAV, switches):
        if uav.is_in_drone_show_mode:
            for switch in switches:
                switch_time = switch[0]
                switch_controller = switch[1]
                await sleep_until(switch_time)
                if uav.is_running_show and uav._airborne:
                    await uav.set_parameter("stabilizer.controller", switch_controller)
        else:
            return
        # await sleep(5)
        # if self.show_clock is not None:
        #     print(self.show_clock().seconds)

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
                handler = DroneHandler(uav, drone_stream, self.log, self.configuration, color=self.colors[uav.id])
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


