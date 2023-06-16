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
from trio import sleep_forever
from .handler import AiMotionMocapFrameHandler
from typing import Dict, Callable, Union, Tuple, Any, List
from flockwave.server.show.trajectory import TrajectorySpecification
from aiocflib.crazyflie.mem import write_with_checksum
from aiocflib.crtp.crtpstack import MemoryType
from aiocflib.utils.checksum import crc32
from aiocflib.crazyflie.log import LogSession
import json
from scipy.signal import butter, filtfilt

__all__ = ("aimotionlab", )

TCP_PORT = 6000


class aimotionlab(Extension):
    """Extension that broadcasts the pose of non-UAV objects to the Crazyflie drones."""
    _hover_traj_defined: Dict[str, bool]
    def __init__(self):
        super().__init__()
        self._active_traj_ID = 2
        # self._hover_traj_defined = False
        self._stream_data = b''
        self._traj = b''
        self._transmission_active = False
        self._load_from_file = False
        self._save_to_local_file = False
        self._memory_partitions = None
        self._block_transmission = False
        self._allow_traj_outside_show = True

    def get_traj_type(self, traj_type: bytes) -> Tuple[bool, Union[bool, None]]:
        # trajectories can either be relative or absolute. This is determined by a string/bytes, but only these two
        # values are allowed. The return tuple tells whether the argument is valid (first part) and if it's
        # relative (second part). If it wasn't valid, we just get None for the second part.
        traj_type_lower = traj_type.lower()  # Bit of an allowance to not be case sensitive
        # TODO: investigate whether the trajectory starts from the drone's current location
        if traj_type_lower == b'relative':
            return True, True
        elif traj_type_lower == b'absolute':
            return True, False
        else:
            return False, None

    def parse(self,
              raw_data: bytes,
              cmd_dict: Dict[bytes, Tuple[Callable[[Extension, CrazyflieUAV, trio.SocketStream, bytes], None], Tuple[bool, Any], bool]]):
        # Function to determine the command and its argument from the TCP port. Standard command structure is like this:
        # b'CMDSTART_ID_command_argument_payload_EOF'. ID refers to the drone that the command must go out to. 0 refers
        # to all drones. The command should be in the command dictionary, which contains data about each command:
        # whether it requires an argument or not, and what kind of argument it requires. Also whether it takes a
        # payload such as the json file containing the trajectory.
        data = raw_data.strip()
        if not data:
            return None, None
        data = data.split(b'_')
        if data[0] != b'CMDSTART':
            return b'NO_CMDSTART', None
        if len(data[1].decode("utf-8")) == 1:  # this means we need to add a leading 0 unfortunately
            ID = "0" + data[1].decode("utf-8")
        else:
            ID = data[1].decode("utf-8")
        command = data[2]
        if command not in cmd_dict:
            return b'WRONG_CMD', None
        if cmd_dict[command][1][0]: # This is a boolean signifying whether we expect an argument
            argument = data[3]
        else:
            argument = None
        if cmd_dict[command][2]: # This is a boolean signifying that we are expecting a payload
            self.log.info("Payload expected.")
            # payload = data[4]
            # self._traj = payload
            # #BUG: WHEN THE WHOLE MESSAGE GETS TRANSMITTED IN ONE, WE GET NO EOF AND UPLOAD DOESNT FINISH
        return ID, command, argument

    async def takeoff(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg):
        try:
            arg = float(arg)
            if arg < 0.1 or arg > 1.5:
                arg = 0.5
                self.log.warning("Takeoff height was out of allowed bounds, taking off to 0.5m")
            if uav._airborne:
                self.log.warning(f"Drone {uav.id} is already airborne, takeoff command wasn't dispatched.")
                await server_stream.send_all(b"Drone is already airborne, takeoff command wasn't dispatched.")
            else:
                await uav.takeoff(altitude=arg)
                await server_stream.send_all(b'Takeoff command dispatched to drone.')
                self.log.info(f"Takeoff command dispatched to drone {uav.id}.")
        except ValueError:
            self.log.warning("Takeoff argument is not a float.")

    async def land(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg):
        if uav._airborne:
            await uav.land()
            await server_stream.send_all(b'Land command dispatched to drone.')
            self.log.info(f"Land command dispatched to drone {uav.id}.")
        else:
            self.log.warning(f"Drone {uav.id} is already on the ground, land command wasn't dispatched.")
            await server_stream.send_all(b"Drone is already on the ground, land command wasn't dispatched.")

    async def start_traj(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg: str):
        cf = uav._get_crazyflie() #access to protected member
        await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=False, reversed=False)
        await server_stream.send_all(b'Trajectory start command received.')

    async def write_safely(self, traj_id: int, handler, data) -> Tuple[bool, Union[int, None]]:
        # Function to upload trajectories to the drone while keeping each trajectory confined to its allotted partition.
        # Check the configuration: For a particular trajectory ID, what is the starting address of the allowed
        # partition, and what's its size? Return with info about whether the trajectory fits into the partition, and the
        # start address. This start address will be slightly different from the beginning of the partition, because the
        # data written to the partition will begin with the checksum, and only then does the trajectory data begin.
        start_addr = self._memory_partitions[traj_id]["start"]
        allowed_size = self._memory_partitions[traj_id]["size"]
        checksum_length = len(crc32(data))
        self.log.info(f"Checksum length: {checksum_length}, data length: {len(data)}, allowed size: {allowed_size}")
        if len(data)+checksum_length <= allowed_size:
            checksum_length = await write_with_checksum(handler, start_addr, data, only_if_changed=True)
            # self.log.info(f"Wrote trajectory to address {start_addr}")
            return True, start_addr+checksum_length
        else:
            return False, None

    async def upload_hover(self, uav: CrazyflieUAV):
        trajectory_data = None
        # Hover trajectory is always the same (0.0 relative setpoint)
        with open('./hover.json') as json_file:
            trajectory_data = json.load(json_file)
        cf = uav._get_crazyflie() #access to protected member
        try:
            trajectory_memory = await cf.mem.find(MemoryType.TRAJECTORY)
        except ValueError:
            raise RuntimeError("Trajectories are not supported on this drone") from None
        trajectory = TrajectorySpecification(trajectory_data)
        data = encode_trajectory(trajectory, encoding=TrajectoryEncoding.COMPRESSED)
        success, addr = await self.write_safely(1, trajectory_memory, data)
        if success:
            await cf.high_level_commander.define_trajectory(1, addr=addr, type=TrajectoryType.COMPRESSED)
            self.log.info(f"Defined fallback hover trajectory for drone {uav.id}!")
        else:
            self.log.warning(f"Trajectory is too long.")
        self._hover_traj_defined[uav.id] = True

    async def handle_new_traj(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg: bytes):
        # Writing to a separate file isn't needed, but helps when debugging.
        if self._save_to_local_file:
            with open('./trajectory.json', 'wb') as f:
                f.write(self._traj)
                self.log.warning("Trajectory saved to local json file for backup.")
        is_valid, is_relative = self.get_traj_type(arg)
        if (uav.is_running_show or self._allow_traj_outside_show) and uav._airborne and is_valid:
            cf = uav._get_crazyflie()  # access to protected member
            # If this is the first trajectory uploaded, we've not yet defined a hover trajectory: do so.
            if not self._hover_traj_defined[uav.id]:
                await self.upload_hover(uav)
            # initiate hover while we switch trajectories so that we don't drift too far
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
            try:
                trajectory_memory = await cf.mem.find(MemoryType.TRAJECTORY)
            except ValueError:
                raise RuntimeError("Trajectories are not supported on this drone") from None
            # We may want to load the trajectory from the backup file:
            if self._load_from_file and self._save_to_local_file:
                with open('./trajectory.json') as json_file:
                    trajectory_data = json.load(json_file)
                self.log.warning("Trajectory read from local json file.")
            else:
                trajectory_data = json.loads(self._traj.decode('utf-8'))
            trajectory = TrajectorySpecification(trajectory_data)
            data = encode_trajectory(trajectory, encoding=TrajectoryEncoding.COMPRESSED)
            # The trajectory IDs we upload to are 2 and 3, we swap between them like so:
            upcoming_traj_ID = 5 - self._active_traj_ID
            # Try to write the data (at this point we don't know whether it's too long or not, write_safely will tell)
            write_success, addr = await self.write_safely(upcoming_traj_ID, trajectory_memory, data)
            if write_success:
                await cf.high_level_commander.define_trajectory(
                    upcoming_traj_ID, addr=addr, type=TrajectoryType.COMPRESSED)
                self.log.info(
                    f"Defined trajectory on ID {upcoming_traj_ID} for drone {uav.id}(currently active ID is {self._active_traj_ID}).")
                await cf.high_level_commander.start_trajectory(upcoming_traj_ID, time_scale=1, relative=is_relative,
                                                               reversed=False)
                self.log.info(f"Started trajectory on ID {upcoming_traj_ID} for drone {uav.id}")
                # We are now playing the trajectory with the new ID: adjust the active ID accordingly.
                self._active_traj_ID = upcoming_traj_ID
            else:
                self.log.warning(f"Trajectory is too long.")
        else:
            self.log.warning(f"Drone {uav.id} is not airborne. Start the hover show to upload trajectories.")
            await server_stream.send_all(b"Drone is not airborne. Start the hover show to upload trajectories.")

        self._block_transmission = True

    async def handle_transmission(self, server_stream: trio.SocketStream):
        if not self._block_transmission:
            await server_stream.send_all(b'Transmission of trajectory started.')
            self.log.info("Transmission of trajectory started.")
            # Find where the json file begins
            start_index = self._stream_data.find(b'{')
            # If the command was 'traj', then a json file must follow. If it doesn't (we can't find the beginning b'{'),
            # then the command or the file was corrupted.
            if start_index == -1:
                self.log.warning("Corrupted trajectory file.")
                return
            else:
                self._traj = self._stream_data[start_index:]
            self._transmission_active = True  # signal we're in the middle of transmission
            while not self._traj.endswith(b'_EOF'):  # receive data until we see that the file has ended
                self._traj += await server_stream.receive_some()  # append the new data to the already received data
            self._traj = self._traj[:-len(b'_EOF')]  # once finished, remove the EOF indicator
            self._transmission_active = False  # then signal that the transmission has ended
            self.log.info("Transmission of trajectory finished.")
            await server_stream.send_all(b'Transmission of trajectory finished.')

    async def traj(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg: bytes):
        await self.handle_transmission(server_stream)
        await self.handle_new_traj(uav, server_stream, arg)

    async def hover(self, uav: CrazyflieUAV, server_stream: trio.SocketStream, arg):
        if not self._hover_traj_defined[uav.id]:
            await self.upload_hover(uav)
        if uav._airborne:
            cf = uav._get_crazyflie()
            await cf.high_level_commander.start_trajectory(1, time_scale=1, relative=True, reversed=False)
            await server_stream.send_all(b'hover command dispatched to drone.')
            self.log.info(f"Hover command dispatched to drone {uav.id}.")
        else:
            self.log.warning(f"Drone {uav.id} is on the ground, if you want to do a takeoff, do so from Live")
            await server_stream.send_all(b"Drone is already on the ground, hover command wasn't dispatched.")
    # The dictionary keeping track of valid commands. Should match up for the client and the server, but we validate
    # whether an incoming command is recognized anyway. Layout is like this:
    # command: (handler_function, (does_it_take_argument, argument_type), does_it_take_payload)
    _tcp_command_dict: Dict[bytes, Tuple[Callable[[Extension, CrazyflieUAV, trio.SocketStream, bytes], None], Tuple[bool, Any], bool]] = {
        b"takeoff": (takeoff, (True, float), False),  # The command takeoff takes a float argument and expects no payload
        b"land": (land, (False, None), False),  # The command land takes no argument and expects no payload
        b"traj": (traj, (True, str), True),  # The command traj takes a str argument and expects a payload
        b"hover": (hover, (False, None), False),
    }

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
        self._memory_partitions = configuration.get("memory_partitions")
        port = configuration.get("port")
        channel = configuration.get("channel")
        signals = self.app.import_api("signals")
        broadcast = self.app.import_api("crazyflie").broadcast
        self.log.info("The new extension is now running.")
        await sleep(1.0)
        self.log.info("One second has passed.")
        if self._save_to_local_file:
            with open('./trajectory.json', 'wb') as f:
                f.write(b'')
            self.log.info("Cleared trajectory.json")

        with ExitStack() as stack:
            # create a dedicated mocap frame handler
            frame_handler = AiMotionMocapFrameHandler(broadcast, port, channel)
            # subscribe to the motion capture frame signal
            stack.enter_context(
                signals.use(
                    {
                        "motion_capture:frame": partial(
                            self._on_motion_capture_frame_received,
                            handler=frame_handler,
                        )
                    }
                )
            )
            # await sleep_forever() # We need *something* here that prevents exiting the context where we are subscribed
            # to the signal. If we don't have the serve_tcp, then we need a sleep forever.
            await trio.serve_tcp(self.TCP_Server, TCP_PORT)

    def _on_motion_capture_frame_received(
            self,
            sender,
            *,
            frame: "MotionCaptureFrame",
            handler: AiMotionMocapFrameHandler,
    ) -> None:
        handler.notify_frame(frame)

    async def cmd_to_single_drone(self, cmd, ID, server_stream, arg):
        self.log.info(f"Command received: {cmd.decode('utf-8')}")  # Let the user know the command arrived
        await server_stream.send_all(b'Command received: ' + cmd)  # Let the client know as well
        try:
            uav: CrazyflieUAV = self.app.object_registry.find_by_id(ID)
            self.log.info(f"UAV {ID} found!")
            # call the appropriate handler function of the command as described in the dictionary
            await self._tcp_command_dict[cmd][0](self, uav, server_stream, arg)
        except KeyError:
            self.log.warning(f"UAV by ID {ID} is not found in the client registry.")
            return

    async def cmd_to_all_drones(self, cmd, server_stream, arg):
        # Check which drones are known by the server
        uav_ids = list(self.app.object_registry.ids_by_type(CrazyflieUAV))
        self.log.info(f"Command received: {cmd.decode('utf-8')}")  # Let the user know the command arrived
        await server_stream.send_all(b'Command received: ' + cmd)  # Let the client know as well
        try:
            # Place the known drones into a list to iterate over
            uavs = [self.app.object_registry.find_by_id(ID) for ID in uav_ids]
            for uav in uavs:
                await self._tcp_command_dict[cmd][0](self, uav, server_stream, arg)
        except KeyError:
            self.log.warning("An UAV was not found in the registry.")
            return

    async def TCP_Server(self, server_stream: trio.SocketStream):
        uav_ids = list(self.app.object_registry.ids_by_type(CrazyflieUAV))
        self._hover_traj_defined = {key: False for key in uav_ids}
        print(self._hover_traj_defined)
        self.log.info(f"Connection made to TCP client. Valid drone IDs: {uav_ids}")
        # uav_ids_bytes = ', '.join(uav_ids).encode("utf-8")
        # await server_stream.send_all(uav_ids_bytes)
        while True:
            # If we're not in the middle of a trajectory's transition, we can handle commands:
            if not self._transmission_active:
                try:
                    self._stream_data: bytes = await server_stream.receive_some()
                    if not self._stream_data:
                        break
                    ID, cmd, arg = self.parse(self._stream_data, self._tcp_command_dict)
                    if cmd == b'NO_CMDSTART':
                        self.log.info(f"Command is missing standard CMDSTART")
                        await server_stream.send_all(b'Command is missing standard CMDSTART')
                    elif cmd == b'WRONG_CMD':
                        self.log.info(f"Command is not found in server side dictionary")
                        await server_stream.send_all(b'Command is not found in server side dictionary')
                    elif cmd is None:
                        self.log.warning(f"None-type command")
                        await server_stream.send_all(b'None-type command')
                    else:
                        # Allow trajectory transmission. This is then blocked after a trajectory is handled to avoid
                        # unnecessarily uploading a trajectory several times when we transmit the trajectory to several
                        # drones at once.
                        self._block_transmission = False
                        if int(ID) == 0:
                            await self.cmd_to_all_drones(cmd, server_stream, arg)
                        else:
                            await self.cmd_to_single_drone(cmd, ID, server_stream, arg)
                except Exception as exc:
                    self.log.warning(f"TCP server crashed: {exc!r}")





