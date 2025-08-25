import time
from typing import List, Tuple, TYPE_CHECKING, Callable, Optional
from functools import partial
from aiocflib.crazyflie import Crazyflie

from aiocflib.crtp.crtpstack import CRTPPort
from aiocflib.crazyflie.localization import (
    GenericLocalizationCommand,
    Localization,
    LocalizationChannel,
)
from aiocflib.utils.quaternion import QuaternionXYZW

from flockwave.server.utils import chunks

from .connection import BroadcasterFunction

if TYPE_CHECKING:
    from .driver import CrazyflieDriver, CrazyflieUAV
    from flockwave.server.ext.motion_capture import MotionCaptureFrame, MotionCaptureFrameItem

__all__ = ("CrazyflieMocapFrameHandler",)


def determine_rigidbody(uav: "CrazyflieUAV", frame: "MotionCaptureFrame") -> Optional["MotionCaptureFrameItem"]:
    """
    Take an uav and a mocap frame, and determine which rigidbody in the frame belongs to that uav.
    If multiple rigidbodies may be associated with the uav, returns the first one, disregarding the rest.
    If no rigidbodies match, returns None.
    The matching is based on the names of the items in the frame and the id of the uav.
    """
    for item in frame.items:
        try:
            if int(uav.id) == int(item.name):
                return item
        except ValueError:
            continue
    return None


class CrazyflieMocapFrameHandler:
    """Handler task that receives frames from mocap systems and dispatches
    the appropriate packets to the corresponding Crazyflie drones.
    """

    _broadcaster: BroadcasterFunction
    _driver: "CrazyflieDriver"

    send_pose: bool
    """Whether to send full pose information if it is available."""

    def __init__(
        self,
        nursery,
        crazyflies_getter
    ):
        """Constructor."""
        self._get_crazyflies: Callable[[], list[CrazyflieUAV]] = crazyflies_getter
        self.nursery = nursery

    def notify_frame(self, frame: "MotionCaptureFrame") -> None:
        for uav in self._get_crazyflies():
            try:
                # might raise RuntimeError if the actual crazyflie isn't connected,
                # but the python object remains in the object registry (e.g. the crazyflie
                # was turned off or restarted)
                cf: Crazyflie = uav._get_crazyflie()
            except RuntimeError:
                # in this case, just skip this cf
                continue
            rigidbody = determine_rigidbody(uav, frame)
            if rigidbody is not None:
                qw, qx, qy, qz = rigidbody.attitude
                x, y, z = rigidbody.position
                port = 6  # CRTP_PORT_LOCALIZATION
                channel = 1  # GENERIC_TYPE
                packet = cf.localization._external_pose_struct.pack(
                    GenericLocalizationCommand.EXT_POSE, x, y, z, qx, qy, qz, qw)
                try:
                    self.nursery.start_soon(partial(cf.send_packet, port=port, channel=channel, data=packet))
                except AttributeError:
                    continue

