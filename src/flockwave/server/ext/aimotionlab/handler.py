from time import  time
from typing import List, Tuple
from aiocflib.crazyflie import Crazyflie
from flockwave.server.utils import chunks
from flockwave.server.ext.motion_capture import MotionCaptureFrame
from aiocflib.utils.quaternion import QuaternionXYZW
from aiocflib.crazyflie.localization import (
    GenericLocalizationCommand,
    Localization,
)


class AiMotionMocapFrameHandler:

    def __init__(self, broadcast, port, channel):
        self._broadcast = broadcast
        self._port = port
        self._channel = channel
        self._cur_id = 0
        self._compress = False

    async def notify_frame(self, frame: "MotionCaptureFrame", crazyflies: List[Crazyflie]):
        # Prefixes which we classify as non-UAV.
        valid_prefixes = ['hook', 'test']
        poses: List[Tuple[int, Tuple[float, float, float], QuaternionXYZW]] = []
        if self._compress:
            for item in frame.items:
                for prefix in valid_prefixes:
                    if item.name.startswith(prefix):
                        try:
                            numeric_id = int(item.name[len(prefix):])
                        except ValueError:
                            continue
                        if item.attitude is not None and item.position is not None:
                            w, x, y, z = item.attitude
                            poses.append((numeric_id + self._cur_id, item.position, QuaternionXYZW(x, y, z, w)))
                            self._cur_id = 1 - self._cur_id
            for chunk in chunks(poses, 2):
                packet = bytes(
                    [GenericLocalizationCommand.EXT_POSE_PACKED]
                ) + Localization.encode_external_pose_packed(chunk)
                # print(f"Broadcasting on channel {self._channel} and port {self._port} with time stamp: {time()}")
                #TODO: when there are two radios available, the drone doesn't receive the correct packets.
                self._broadcast(
                    self._port,
                    self._channel,
                    packet,
                )
        else:
            for item in frame.items:
                for prefix in valid_prefixes:
                    if item.name.startswith(prefix):
                        try:
                            numeric_id = int(item.name[len(prefix):])
                        except ValueError:
                            continue
                        if item.attitude is not None and item.position is not None:
                            qw, qx, qy, qz = item.attitude
                            x, y, z = item.position
                            if crazyflies is not None:
                                for crazyflie in crazyflies:
                                    packet = crazyflie.localization._external_pose_struct.pack(
                                        GenericLocalizationCommand.EXT_POSE, x, y, z, qx, qy, qz, qw)
                                    await crazyflie.send_packet(port=self._port, channel=self._channel, data=packet)
                                    # print(f"sending to {crazyflie.uri}")


