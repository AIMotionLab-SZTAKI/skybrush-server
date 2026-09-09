import trio
import struct
from typing import Tuple, Callable, Any, List, Optional, Dict, Union
from trio import sleep, sleep_until
import json
import time
from functools import partial


def warning(text: str):
    color = "\033[33m"
    reset = "\033[0m"
    print(f"{color}[{time.time()-start_time:.3f} WARNING] {text}{reset}")


def log(text: str):
    color = "\033[36m"
    reset = "\033[0m"
    print(f"{color}[{time.time() - start_time:.3f} LOG] {text}{reset}")


# Mirror of the "memory_partitions" block of skybrushd.jsonc. There is no drone here to write to, so only the
# partition IDs matter: they drive the same ping-pong between the two dynamic partitions that the real
# DroneHandler performs. Keep in sync with skybrushd.jsonc.
MEMORY_PARTITIONS: List[Dict[str, Any]] = [
    {"ID": 0, "size": 100, "start": 1, "dynamic": False},
    {"ID": 1, "size": 100, "start": 104, "dynamic": False},
    {"ID": 2, "size": 3980, "start": 208, "dynamic": True},
    {"ID": 3, "size": 3980, "start": 4200, "dynamic": True},
]


class DroneHandler:
    def __init__(self, uav_id: str, stream: trio.SocketStream, color):
        self.uav_id = uav_id
        self.stream = stream
        self.transmission_active = False
        self.stream_data = b''
        self.traj = b''
        self.color = color
        self.crashed = False
        self.hover_defined = False
        # The real handler asks its UAV object whether it is airborne. We have no UAV, so we track it ourselves,
        # in order to accept and refuse the same commands the real handler would.
        self.airborne = False
        self.active_traj_ID: int = max(partition["ID"] for partition in MEMORY_PARTITIONS)
        self.traj_ID_sum: int = sum(partition["ID"] for partition in MEMORY_PARTITIONS if partition["dynamic"])

    def upcoming_traj(self):
        return self.traj_ID_sum - self.active_traj_ID

    def print(self, text):
        reset_color = "\033[0m"
        print(f"{self.color}[drone_{self.uav_id}]: {text}{reset_color}")

    def warning(self, text):
        warning(f"[drone_{self.uav_id}]: {text}")

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

    @staticmethod
    def get_traj_type(self, arg: bytes) -> Tuple[bool, Union[bool, None]]:
        # trajectories can either be relative or absolute. This is determined by a string/bytes, but only these two
        # values are allowed. The return tuple tells whether the argument is valid (first part) and if it's
        # relative (second part). If it wasn't valid, we just get None for the second part.
        traj_type_lower = arg.lower()  # Bit of an allowance to not be case sensitive
        if traj_type_lower == b'relative' or traj_type_lower == b'rel':
            return True, True
        elif traj_type_lower == b'absolute' or traj_type_lower == b'abs':
            return True, False
        else:
            return False, None

    async def handle_transmission(self):
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

    async def command(self, cmd: bytes, arg: bytes):
        self.print(f"Command received: {cmd.decode('utf-8')}")
        await self.tcp_command_dict[cmd][0](self, arg)

    async def define_hover(self):
        # The real handler uploads hover.json to trajectory ID 1 here. There is nothing to upload it to, but we do
        # read the file, so that a missing or broken hover.json fails here just like it would on the real server.
        with open('./hover.json') as json_file:
            json.load(json_file)
        self.print("Defined fallback hover.")
        self.hover_defined = True

    async def takeoff(self, arg: bytes):
        try:
            height = float(arg)
            if height < 0.1 or height > 1.5:
                self.warning(f"Takeoff height {height}m is out of allowed range, taking off to 0.5m instead.")
                height = 0.5
            if self.airborne:
                self.warning(f"Already airborne, takeoff command wasn't dispatched.")
                self.crashed = True
            else:
                await sleep(0.01)
                self.airborne = True
                self.print(f"Takeoff command dispatched, height={height}")
                await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        except ValueError:
            self.warning("Takeoff argument is not a float.")
            self.crashed = True
        except Exception as exc:
            self.warning(f"Couldn't take off because of this exception: {exc!r}. ")
            self.crashed = True

    async def land(self, arg: bytes):
        if self.airborne:
            self.airborne = False
            self.print(f"Land command dispatched.")
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Already on the ground, land command wasn't dispatched.")
            self.crashed = True

    async def upload(self, arg: bytes):
        await self.handle_transmission()
        try:
            trajectory_data = json.loads(self.traj.decode('utf-8'))
        except Exception as exc:
            self.warning(f"Trajectory couldn't be written: {exc!r}")
            await self.stream.send_all(b'ERR')  # reply with error message
            self.airborne = False  # the real handler lands the drone at this point
            self.crashed = True
            return
        traj_type = trajectory_data.get("type", "COMPRESSED")
        # The real handler encodes the trajectory and refuses it if it does not fit into its memory partition.
        # We cannot measure the encoded size without the server's encoder, so an upload never fails on size here.
        self.print(f"Defined {traj_type} trajectory on ID {self.upcoming_traj()} "
                   f"(currently active ID is {self.active_traj_ID}).")
        await self.stream.send_all(b'ACK')  # reply with an acknowledgement

    async def start(self, arg: bytes):
        is_valid, is_relative = self.get_traj_type(self, arg=arg)
        if is_valid:
            self.print(f"Started {'relative' if is_relative else 'absolute'} trajectory "
                       f"on ID {self.upcoming_traj()}.")
            # We are now playing the trajectory with the new ID: adjust the active ID accordingly.
            self.active_traj_ID = self.upcoming_traj()
            await self.stream.send_all(b'ACK')  # reply with an acknowledgement
        else:
            self.warning(f"Invalid trajectory type: {arg!r}")
            self.crashed = True

    async def hover(self, arg: bytes):
        if not self.hover_defined:
            await self.define_hover()
        if self.airborne:
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
            self.print(f"Set {param} to {value}")
        except Exception as exc:
            self.warning(f"Exception while setting parameter: {exc!r}")
        # failure to set a parameter usually doesn't result in catastrophic failure so reply anyway
        await self.stream.send_all(b'ACK')

    tcp_command_dict: Dict[
        bytes, Tuple[Callable[[Any, bytes], None], bool]] = {
        b"takeoff": (takeoff, True),
        b"land": (land, False),
        b"upload": (upload, True),
        b"hover": (hover, False),
        b"start": (start, True),
        b"param": (set_param, True)
    }

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


async def listen_and_broadcast(stream: trio.SocketStream, *,port: int, streams: List[trio.SocketStream]):
    streams.append(stream)
    print(f"Number of connections on port {port} changed to {len(streams)}")
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
    print(f"Number of connections on port {port} changed to {len(streams)}")


async def establish_drone_handler(stream: trio.SocketStream, *, handlers: List[DroneHandler]):
    taken_ids = [handler.uav_id for handler in handlers]
    available_ids = [drone_id for drone_id in uav_ids if drone_id not in taken_ids]
    if len(available_ids) != 0:
        log(f"TCP connection made. Valid drone IDs: {uav_ids}. "
              f"Of these the following are not yet taken: {available_ids}")
        request = await stream.receive_some()
        request = request.decode('utf-8')
        if 'REQ_' in request:
            requested_id = request.split('REQ_')[1]
            if requested_id not in available_ids:
                warning(f"ID {requested_id} taken already.")
                await stream.send_all(b'ACK_00')
                return
            color = colors[requested_id] if requested_id in colors else "\033[92m"
            handler = DroneHandler(requested_id, stream, color=color)
            handlers.append(handler)
            log(f"Made handler for drone {requested_id}. The following drones have handlers: {[handler.uav_id for handler in handlers]}")
            acknowledgement = f"ACK_{requested_id}"
            await stream.send_all(acknowledgement.encode('utf-8'))
            await handler.listen()
            handlers.remove(handler)
            log(f"Removing handler for drone {handler.uav_id}. "
                  f"Remaining handlers: {[handler.uav_id for handler in handlers]}")
        else:
            warning(f"Wrong request.")
            await stream.send_all(b'ACK_00')
            return
    else:
        warning("All drone IDs are accounted for.")
        await stream.send_all(b'ACK_00')
        return

uav_ids = ["04", "06", "07", "08", "09"]
uav_ids = uav_ids + [str(i) for i in range(10, 99)]
handlers: List[DroneHandler] = []
car_streams: List[trio.SocketStream] = []
simulation_streams: List[trio.SocketStream] = []
start_time = time.time()
log("DUMMY SERVER READY! :)")
# Same port numbers as the "tcp_ports" block of skybrushd.jsonc, so that client scripts need no edits when they
# are pointed at the dummy server. This does mean that the two cannot run at the same time. The "lqr" port (6003)
# is not served here: its handler streams log variables from a real drone, which cannot be faked usefully.
PORT = 6000
colors = {"04": "\033[92m",
          "06": "\033[93m",
          "07": "\033[94m",
          "08": "\033[96m",
          "09": "\033[95m"}

ports: List[Tuple[int, Callable]] = [(PORT, partial(establish_drone_handler, handlers=handlers)),
                                     (PORT+1, partial(listen_and_broadcast, port=PORT+1, streams=car_streams)),
                                     (PORT + 2, partial(listen_and_broadcast, port=PORT+2, streams=simulation_streams))]

async def TCP_parent():
    async with trio.open_nursery() as nursery:
        for port, func in ports:
            # func is partial(establish_drone_handler, handlers=handlers), with one positional argument: stream
            serve_tcp = partial(trio.serve_tcp, handler=func, port=port, handler_nursery=nursery)
            nursery.start_soon(serve_tcp)
        start = None
        while start != "start":
            start = await trio.to_thread.run_sync(input, 'Type "start" to simulate a skyc start!\n')
        try:
            # The same notifications the aimotionlab extension registers in its configure() method.
            for stream in car_streams:
                print("STARTING CAR WROOM WROOM")
                await stream.send_all(struct.pack("f", 5.5))
            for stream in simulation_streams:
                print("START SIMULATION!")
                await stream.send_all(b'START')
        except Exception as exc:
            print(f"Exception: {exc!r}")
trio.run(TCP_parent)
