import logging
import trio
from trio import sleep
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
from typing import Dict, Callable, Union, Tuple, Any, List
from flockwave.server.show.trajectory import TrajectorySpecification
from aiocflib.crazyflie.mem import write_with_checksum
from aiocflib.crtp.crtpstack import MemoryType
from aiocflib.utils.checksum import crc32
import json

__all__ = ("aimotionlab", )

#TODO: make a restart command, and use it to handle takeoff commands when the drone is exactly in 0, 0, 0
class DroneHandler:
    def __init__(self, uav: CrazyflieUAV, stream: trio.SocketStream, log: logging.Logger, configuration):
        self.traj = b''
        self.active_traj_ID = 2
        self.stream_data = b''
        self.transmission_active = False
        self.allow_traj_outside_show = True
        self.uav = uav
        self.stream = stream
        self.log = log
        self.hover_between = False
        self.memory_partitions = configuration.get("memory_partitions")

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
            self.log.info(f"Defined fallback hover trajectory for drone {self.uav.id}!")
        else:
            self.log.warning(f"Trajectory is too long.")

    @staticmethod
    def get_traj_type(self, arg: bytes) -> Tuple[bool, Union[bool, None]]:
        # trajectories can either be relative or absolute. This is determined by a string/bytes, but only these two
        # values are allowed. The return tuple tells whether the argument is valid (first part) and if it's
        # relative (second part). If it wasn't valid, we just get None for the second part.
        traj_type_lower = arg.lower()  # Bit of an allowance to not be case sensitive
        # TODO: investigate whether the trajectory starts from the drone's current location
        if traj_type_lower == b'relative':
            return True, True
        elif traj_type_lower == b'absolute':
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
        self.log.info(f"drone{self.uav.id}: Checksum length: {checksum_length}, data length: {len(data)}, allowed size: {allowed_size}")
        if (len(data) + checksum_length <= allowed_size) and traj_id != self.active_traj_ID:
            # try:
            #     checksum_length = await write_with_checksum(handler, start_addr, data, only_if_changed=True)
            #     self.log.info(f"drone{self.uav.id}: Wrote trajectory to address {start_addr}")
            #     return True, start_addr + checksum_length
            # except Exception as exc:
            #     self.log.warning(f"drone{self.uav.id}: Couldn't write because of this exception: {exc!r}. "
            #                      f"Trying again in 0.1 second.")
            with trio.move_on_after(2.5):
                while True:
                    try:
                        checksum_length = await write_with_checksum(handler, start_addr, data, only_if_changed=True)
                        self.log.info(f"drone{self.uav.id}: Wrote trajectory to address {start_addr}")
                        return True, start_addr + checksum_length
                    except Exception as exc:
                        self.log.warning(f"drone{self.uav.id}: Couldn't write because of this exception: {exc!r}. "
                                         f"Trying again in 0.05 second.")
                        await sleep(0.05)
            self.log.warning(f"drone{self.uav.id}: Moved on after 2.5 second of trying to write trajectory.")
        return False, None

    async def takeoff(self, arg: bytes):
        try:
            arg = float(arg)
            if arg < 0.1 or arg > 1.5:
                arg = 0.5
                self.log.warning(f"Takeoff height {arg}m is out of allowed range, taking off to 0.5m instead.")
            if self.uav._airborne:
                self.log.warning(f"Drone {self.uav.id} already airborne, takeoff command wasn't dispatched.")
                # await self.stream.send_all(b"Drone is already airborne, takeoff command wasn't dispatched.")
            else:
                await self.uav.takeoff(altitude=arg)
                # await self.stream.send_all(b'Takeoff command dispatched to drone.')
                self.log.info(f"Takeoff command dispatched to drone {self.uav.id}.")
                await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        except ValueError:
            self.log.warning("Takeoff argument is not a float.")
        except Exception as exc:
            self.log.warning(f"drone{self.uav.id}: Couldn't take off because of this exception: {exc!r}. ")


    async def land(self, arg: bytes):
        if self.uav._airborne:
            await self.uav.land()
            # await self.stream.send_all(b'Land command dispatched to drone.')
            self.log.info(f"Land command dispatched to drone {self.uav.id}.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.log.warning(f"Drone {self.uav.id} is already on the ground, land command wasn't dispatched.")
            # await self.stream.send_all(b"Drone is already on the ground, land command wasn't dispatched.")

    async def handle_transmission(self):
        # await self.stream.send_all(b'Transmission of trajectory started.')
        self.log.info(f"drone{self.uav.id}: Transmission of trajectory started.")
        start_index = self.stream_data.find(b'{')
        # If the command was 'traj', then a json file must follow. If it doesn't (we can't find the beginning b'{'),
        # then the command or the file was corrupted.
        if start_index == -1:
            self.log.warning("Corrupted trajectory file.")
        else:
            self.traj = self.stream_data[start_index:]
            self.transmission_active = True  # signal we're in the middle of transmission
            while not self.traj.endswith(b'_EOF'):  # receive data until we see that the file has ended
                self.traj += await self.stream.receive_some()  # append the new data to the already received data
            self.traj = self.traj[:-len(b'_EOF')]  # once finished, remove the EOF indicator
            self.transmission_active = False  # then signal that the transmission has ended
            self.log.info(f"drone{self.uav.id}: Transmission of trajectory finished.")
            # await self.stream.send_all(b'Transmission of trajectory finished.')

    async def upload(self, arg: bytes):
        await self.handle_transmission()
        cf = self.uav._get_crazyflie()
        if self.hover_between: # TODO: where should this be???? definitely not here
            # initiate hover while we switch trajectories so that we don't drift too far
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
        try:
            trajectory_memory = await cf.mem.find(MemoryType.TRAJECTORY)
        except ValueError:
            raise RuntimeError("Trajectories are not supported on this drone") from None
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
            self.log.info(
                f"Defined trajectory on ID {upcoming_traj_ID} for drone {self.uav.id}"
                f"(currently active ID is {self.active_traj_ID}).")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.log.warning(f"Trajectory couldn't be written.")

    async def start(self, arg: bytes):
        cf = self.uav._get_crazyflie()
        is_valid, is_relative = self.get_traj_type(self, arg=arg)
        upcoming_traj_ID = 5 - self.active_traj_ID
        if (self.uav.is_running_show or self.allow_traj_outside_show) and self.uav._airborne and is_valid:
            await cf.high_level_commander.start_trajectory(upcoming_traj_ID, time_scale=1, relative=is_relative,
                                                           reversed=False)
            self.log.info(f"Started trajectory on ID {upcoming_traj_ID} for drone {self.uav.id}")
            # We are now playing the trajectory with the new ID: adjust the active ID accordingly.
            self.active_traj_ID = upcoming_traj_ID
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.log.warning(f"Drone {self.uav.id} is not airborne.")

    async def hover(self, arg: bytes):
        if self.uav._airborne:
            cf = self.uav._get_crazyflie()
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
            self.log.info(f"Hover command dispatched to drone {self.uav.id}.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.log.warning(f"Drone {self.uav.id} is on the ground, if you want to do a takeoff, do so from Live")

    async def command(self, cmd: bytes, arg: bytes):
        self.log.info(f"Command received for drone {self.uav.id}: {cmd.decode('utf-8')}")
        # await self.stream.send_all(b'Command received: ' + cmd)
        await self.tcp_command_dict[cmd][0](self, arg)

    tcp_command_dict: Dict[
        bytes, Tuple[Callable[[Any, bytes], None], Tuple[bool, Any], bool]] = {
        b"takeoff": (takeoff, (True, float), False),
        # The command takeoff takes a float argument and expects no payload
        b"land": (land, (False, None), False),  # The command land takes no argument and expects no payload
        b"upload": (upload, (False, None), True),  # The command upload takes no argument but expects a payload
        b"hover": (hover, (False, None), False),  # The command hover takes no argument and expects no payload
        b"start": (start, (True, str), False),  # The command start a string argument but expects no payload
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
        if self.tcp_command_dict[command][1][0]:  # This is a boolean signifying whether we expect an argument
            argument = data[2]
        else:
            argument = None
        if self.tcp_command_dict[command][2]:   # This is a boolean signifying that we are expecting a payload
            pass  # not needed? verify pls
        return command, argument

    async def listen(self):
        while True:
            if not self.transmission_active:
                try:
                    self.stream_data: bytes = await self.stream.receive_some()
                    if not self.stream_data:
                        break
                    cmd, arg = self.parse(self.stream_data)
                    if cmd == b'NO_CMDSTART':
                        self.log.info(f"Command for drone {self.uav.id} is missing standard CMDSTART")
                        # await self.stream.send_all(b'Command is missing standard CMDSTART')
                        break
                    elif cmd == b'WRONG_CMD':
                        self.log.info(f"Command for drone {self.uav.id} is not found in server side dictionary")
                        # await self.stream.send_all(b'Command is not found in server side dictionary')
                        break
                    elif cmd is None:
                        self.log.warning(f"None-type command for drone {self.uav.id}")
                        # await self.stream.send_all(b'None-type command')
                        break
                    else:
                        await self.command(cmd, arg)
                except Exception as exc:
                    self.log.warning(f"TCP handler for drone {self.uav.id} crashed: {exc!r}")
                    break


class aimotionlab(Extension):
    """Extension that broadcasts the pose of non-UAV objects to the Crazyflie drones."""
    drone_handlers: List[DroneHandler]

    def __init__(self):
        super().__init__()
        self.drone_handlers = []
        self.configuration = None

    def _crazyflies(self):
        uav_ids = list(self.app.object_registry.ids_by_type(CrazyflieUAV))
        uavs: List[CrazyflieUAV] = []
        for uav_id in uav_ids:
            uavs.append(self.app.object_registry.find_by_id(uav_id))
        return [uav._get_crazyflie() for uav in uavs]

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
        TCP_PORT = configuration.get("TCP_Port", 6000)
        port = configuration.get("cf_port", 1)
        channel = configuration.get("channel")
        signals = self.app.import_api("signals")
        broadcast = self.app.import_api("crazyflie").broadcast
        self.log.info("The new extension is now running.")
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
                            )
                        }
                    )
                )
                await trio.serve_tcp(self.establish_drone_handler, TCP_PORT, handler_nursery=nursery)

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
        # take away the ones that already have a handler TODO: provide a way to relinquish command
        taken_ids = [handler.uav.id for handler in self.drone_handlers]
        # and what's left is the drones up for grabs
        available_ids = [drone_id for drone_id in uav_ids if drone_id not in taken_ids]
        if len(available_ids) != 0:  # if there are free drones, tell the user
            self.log.info(f"TCP connection made. Valid drone IDs: {uav_ids}. "
                          f"Of these the following are not yet taken: {available_ids}")
            available_ids_string = ",".join(available_ids)
            available_ids_bytes = available_ids_string.encode("utf-8")
            await drone_stream.send_all(available_ids_bytes)
            requested_id = await drone_stream.receive_some()
            requested_id = requested_id.decode("utf-8")
            try:
                uav: CrazyflieUAV = self.app.object_registry.find_by_id(requested_id)
                handler = DroneHandler(uav, drone_stream, self.log, self.configuration)
                self.drone_handlers.append(handler)
                self.log.info(f"Made handler for drone {requested_id}. "
                              f"Currently we have handlers for the following drones: "
                              f"{[handler.uav.id for handler in self.drone_handlers]}")
                await handler.define_hover()
                await drone_stream.send_all(b'success')
                await handler.listen()
                self.drone_handlers = [drone_handler for drone_handler in self.drone_handlers
                                       if drone_handler.uav.id != handler.uav.id]
                self.log.info(f"Removing handler for drone {handler.uav.id}. Remaining handlers:"
                              f"{[drone_handler.uav.id for drone_handler in self.drone_handlers]}")
            except KeyError:
                self.log.warning(f"UAV by ID {requested_id} is not found in the client registry.")
        else:  # if there aren't any drones left, tell them that
            self.log.warning(f"All drone IDs are accounted for.")
            await drone_stream.send_all(b'00')



