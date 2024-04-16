import logging
import trio
from flockwave.server.ext.crazyflie.driver import CrazyflieUAV
from flockwave.server.ext.crazyflie.trajectory import encode_trajectory, TrajectoryEncoding
from aiocflib.crazyflie.high_level_commander import TrajectoryType
from typing import Dict, Callable, Union, Tuple, Any, List
from flockwave.server.show.trajectory import TrajectorySpecification
from aiocflib.crtp.crtpstack import MemoryType
from aiocflib.utils.checksum import crc32
import json
from aiocflib.crazyflie.mem import MemoryHandler, MemoryHandlerBase, MemoryChannel
from aiocflib.crtp.crtpstack import MemoryType, CRTPPort
from aiocflib.utils.checksum import crc32
from aiocflib.utils.chunks import chunkify
from aiocflib.errors import error_to_string
from errno import ENODATA
import sys


async def write(self, addr: int, data: bytes, timeout: float = 0.2, attempts: int = 3) -> None:
    """This function is what is monkey-patched to be used instead of MemoryHandlerBase.write.
    The only difference is that it can take two extra arguments: timeout and attemps, so that we are able to
    manually set these if we wish to."""
    for start, size in chunkify(
            0, len(data), step=MemoryHandler.MAX_WRITE_REQUEST_LENGTH
    ):
        if addr != 0:
            sys.stdout.write(f"\rUpload progress: {int((start+size)/len(data)*100)}%: \t{'#'*int(start/len(data)*60)}")
            sys.stdout.flush()
        status = await self._write_chunk(addr + start, data[start: (start + size)], timeout=timeout, attempts=attempts)
        if status:
            raise IOError(
                status,
                "Write request returned error code {0} ({1})".format(
                    status, error_to_string(status)
                ),
            )
    if addr != 0:
        print()


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
        self.stream_data = b''
        self.transmission_active = False
        self.allow_traj_outside_show = True
        self.uav = uav
        self.stream = stream
        self.log = log
        self.hover_between = False
        self.hover_defined = False
        self.memory_partitions: List[Dict] = configuration.get("memory_partitions")
        self.active_traj_ID: int = max(partition["ID"] for partition in self.memory_partitions)
        self.traj_ID_sum: int = sum(partition["ID"] for partition in self.memory_partitions if partition["dynamic"])
        self.crashed = False
        self.color = color
        # Memory partitions are a list of dictionaries. However, we may not have the IDs in order of
        # how they were stored in skybrushd.jsonc, so we reformat it to a dictionary
        self.memory_partitions: Dict[int, Dict] = {partition["ID"]: {"size": partition["size"],
                                                                     "start": partition["start"]}
                                                   for partition in self.memory_partitions}

    def upcoming_traj(self):
        return self.traj_ID_sum - self.active_traj_ID

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
                checksum_length = await write_with_checksum(handler, start_addr, data, only_if_changed=True, timeout=1.25, attempts=2)
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
                self.print(f"Takeoff command dispatched, height={arg}")
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
            self.crashed = True
            raise RuntimeError("Trajectories are not supported on this drone") from None
        trajectory_data = json.loads(self.traj.decode('utf-8'))
        trajectory = TrajectorySpecification(trajectory_data)
        traj_type = trajectory._data.get("type", "COMPRESSED")
        encoding = TrajectoryEncoding.POLY4D if traj_type == "POLY4D" else TrajectoryEncoding.COMPRESSED
        data = encode_trajectory(trajectory, encoding=encoding)
        # Try to write the data (at this point we don't know whether it's too long or not, write_safely will tell)
        write_success, addr = await self.write_safely(self.upcoming_traj(), trajectory_memory, data)
        if write_success:
            if encoding == TrajectoryEncoding.POLY4D:
                await cf.high_level_commander.define_trajectory(
                    self.upcoming_traj(), addr=addr, num_pieces=len(trajectory._data.get("points"))-1, type=TrajectoryType.POLY4D
                )
                await cf.high_level_commander.set_group_mask()
            else:
                await cf.high_level_commander.define_trajectory(
                    self.upcoming_traj(), addr=addr, type=TrajectoryType.COMPRESSED)
            self.print(
                f"Defined {traj_type} trajectory on ID {self.upcoming_traj()} (currently active ID is {self.active_traj_ID}).")
            self.warning(f"Writing took {trio.current_time() - upload_time} sec.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Trajectory couldn't be written.")
            await self.uav.land()
            self.crashed = True

    async def start(self, arg: bytes):
        cf = self.uav._get_crazyflie()
        is_valid, is_relative = self.get_traj_type(self, arg=arg)
        if (self.uav.is_running_show or self.allow_traj_outside_show) and is_valid:
            await cf.high_level_commander.start_trajectory(self.upcoming_traj(), time_scale=1, relative=is_relative,
                                                           reversed=False)
            self.print(f"Started trajectory on ID {self.upcoming_traj()}.")
            # We are now playing the trajectory with the new ID: adjust the active ID accordingly.
            self.active_traj_ID = self.upcoming_traj()
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

    async def set_param(self, arg: bytes):
        # arg should look something like this: b'stabilizer.controller=1'
        try:
            param, value = arg.split(b'=')
            param = param.decode()
            value = float(value)
            if param != await self.uav.get_parameter(param):
                await self.uav.set_parameter(param, value)
                self.print(f"Set {param} to {value}")
        except Exception as exc:
            self.warning(f"Exception while setting parameter: {exc!r}")
        # failure to set a parameter usually doesn't result in catastrophic failure so reply anyway
        await self.stream.send_all(b'ACK')

    async def command(self, cmd: bytes, arg: bytes):
        self.print(f"Command received: {cmd.decode('utf-8')}")
        # await self.stream.send_all(b'Command received: ' + cmd)
        await self.tcp_command_dict[cmd][0](self, arg)

    tcp_command_dict: Dict[
        bytes, Tuple[Callable[[Any, bytes], None], bool]] = {
        b"takeoff": (takeoff, True),
        b"land": (land, False),
        b"upload": (upload, True),
        b"hover": (hover, False),
        b"start": (start, True),
        b"param": (set_param, True)
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
