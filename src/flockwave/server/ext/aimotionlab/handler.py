from time import time
from typing import List, Tuple
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
        self._prev_poses: List[List[Tuple[int, Tuple[float, float, float], QuaternionXYZW]]] = []

    def notify_frame(self, frame: "MotionCaptureFrame"):
        # Prefixes which we classify as non-UAV.
        valid_prefixes = ['bu', 'hook', 'test']
        poses: List[Tuple[int, Tuple[float, float, float], QuaternionXYZW]] = []
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
