"""Relay that streams external (e.g. MPC) body rate + thrust commands from a TCP client to one drone.

Protocol, from the client's side:

1. Send ``REQ_<drone id>[_<policy>[_<arg>]]`` as UTF-8 text, e.g. ``REQ_07``, ``REQ_07_land_0.3`` or
   ``REQ_07_hover_2.0``. The policy decides what happens when the session ends (see below):

   - ``land`` (default): land with the given descent velocity [m/s] (default 0.5).
   - ``hover``: hold the current position for the given number of seconds (default 2.0), then land.
   - ``none``: do nothing, the firmware's own timeout decides what happens.

2. Wait for ``ACK_<drone id>``, or ``ACK_00`` if the drone is not available. Numeric IDs match regardless of
   leading zeros, and the ACK carries the ID as the server knows it: ``REQ_8`` is answered with ``ACK_08``.
3. Send fixed size binary frames, each of which is forwarded to the drone unchanged as a generic setpoint
   (CRTP port 7, channel 0). A frame is ``struct.pack("<BBffffff", 20, status, thrust, roll_rate, pitch_rate,
   yaw_rate, com_x, com_y)``: thrust in N, rates in deg/s, center of mass shift in m.
4. To end the session, either send the single byte ``0x00``, or close the connection.

The relay is latest-wins: if frames arrive faster than the radio can send them, only the newest one is sent. When
the session ends (end byte, disconnect, or no frame for longer than ``STREAM_TIMEOUT`` after the first frame), the
server hands the drone back to the high-level commander, and executes the policy chosen in the handshake.
"""

import struct
from logging import Logger
from typing import Iterable, Optional, Tuple

import trio
from aiocflib.crazyflie import Crazyflie

from flockwave.server.ext.crazyflie.driver import CrazyflieUAV

__all__ = ("MpcSession", "parse_mpc_request", "resolve_uav_id", "MPC_FRAME_SIZE")

MPC_SETPOINT_TYPE = 20
"""Generic setpoint type of the MPC command, see crtp_commander_generic.c in the firmware."""

MPC_END_SESSION = 0x00
"""Single byte a client may send to end the session cleanly."""

MPC_FRAME_SIZE = struct.calcsize("<BBffffff")  # 26 bytes, fits into one CRTP packet (max 30)

CRTP_PORT_SETPOINT_GENERIC = 7
SET_SETPOINT_CHANNEL = 0
META_COMMAND_CHANNEL = 1
META_NOTIFY_SETPOINTS_STOP = 0

RX_COUNTER_PARAMETER = "bodyrate.mpc_rx"
"""Read-only firmware parameter counting the MPC commands the controller consumed."""

STREAM_TIMEOUT = 0.25
"""If no frame arrives for this long (after the first one), the session is ended."""

POLICIES = {"land": 0.5, "hover": 2.0, "none": None}
"""Allowed session end policies, and the default value of their argument."""


def parse_mpc_request(request: str) -> Tuple[str, str, Optional[float]]:
    """Parses ``REQ_<id>[_<policy>[_<arg>]]`` into (id, policy, arg). Raises ValueError if malformed."""
    parts = request.strip().split("_")
    if len(parts) < 2 or parts[0] != "REQ" or not parts[1]:
        raise ValueError(f"Malformed request: {request!r}")
    uav_id = parts[1]
    policy = parts[2].lower() if len(parts) > 2 else "land"
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy: {policy!r}")
    arg = float(parts[3]) if len(parts) > 3 else POLICIES[policy]
    return uav_id, policy, arg


def resolve_uav_id(requested: str, uav_ids: Iterable[str]) -> Optional[str]:
    """Finds the drone ID the client meant: an exact match, or else a numeric match, so that "8" finds "08"."""
    uav_ids = list(uav_ids)
    if requested in uav_ids:
        return requested
    try:
        number = int(requested)
    except ValueError:
        return None
    for uav_id in uav_ids:
        try:
            if int(uav_id) == number:
                return uav_id
        except ValueError:
            continue
    return None


class MpcSession:
    """One client streaming commands to one drone."""

    def __init__(self, uav: CrazyflieUAV, stream: trio.SocketStream, log: Logger, policy: str,
                 policy_arg: Optional[float]):
        self.uav = uav
        self.cf: Crazyflie = uav._get_crazyflie()
        self.stream = stream
        self.log = log
        self.policy = policy
        self.policy_arg = policy_arg
        self._latest: Optional[bytes] = None
        self._new_frame = trio.Event()
        self.frames_received = 0
        self.frames_sent = 0

    async def run(self) -> None:
        """Relays frames until the session ends, then executes the end of session policy."""
        reason = "unknown"
        rx_before = await self._read_rx_counter()
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._send_frames)
                reason = await self._receive_frames()
                nursery.cancel_scope.cancel()
        finally:
            self.log.info(f"MPC session of drone {self.uav.id} ended ({reason}), received {self.frames_received}, "
                          f"sent {self.frames_sent} frames. Policy: {self.policy}.")
            # Hand the drone back even if we are being cancelled, e.g. by server shutdown.
            with trio.CancelScope(shield=True):
                await self._end_session()
                await self._report_rx_counter(rx_before)

    async def _receive_frames(self) -> str:
        """Reads frames into the latest-wins slot. Returns the reason why the session ended."""
        buffer = bytearray()
        while True:
            # The timeout only applies once streaming started: the client may take its time with the first frame.
            timeout = STREAM_TIMEOUT if self.frames_received > 0 else float("inf")
            with trio.move_on_after(timeout):
                try:
                    data = await self.stream.receive_some()
                except (trio.BrokenResourceError, trio.ClosedResourceError):
                    return "connection lost"
                if not data:
                    return "client disconnected"
                buffer += data
                while buffer:
                    if buffer[0] == MPC_END_SESSION:
                        return "end of session requested"
                    if buffer[0] != MPC_SETPOINT_TYPE:
                        return f"invalid frame type {buffer[0]}"
                    if len(buffer) < MPC_FRAME_SIZE:
                        break  # wait for the rest of the frame
                    self._latest = bytes(buffer[:MPC_FRAME_SIZE])
                    del buffer[:MPC_FRAME_SIZE]
                    self.frames_received += 1
                    self._new_frame.set()
                continue
            return f"no frame for {STREAM_TIMEOUT * 1000:.0f} ms"

    async def _send_frames(self) -> None:
        """Sends the newest frame whenever there is one. Frames overwritten while the radio was busy are dropped."""
        while True:
            await self._new_frame.wait()
            self._new_frame = trio.Event()
            await self.cf.send_packet(port=CRTP_PORT_SETPOINT_GENERIC, channel=SET_SETPOINT_CHANNEL,
                                      data=self._latest)
            self.frames_sent += 1

    async def _read_rx_counter(self) -> Optional[int]:
        """Reads how many MPC commands the firmware has consumed so far, or None if it cannot tell."""
        with trio.move_on_after(0.5):
            try:
                return int(await self.uav.get_parameter(RX_COUNTER_PARAMETER, fetch=True))
            except Exception:
                return None
        return None

    async def _report_rx_counter(self, rx_before: Optional[int]) -> None:
        """Logs how many of the frames sent over the radio the firmware actually consumed."""
        if rx_before is None or self.frames_sent == 0:
            return
        rx_after = await self._read_rx_counter()
        if rx_after is None:
            return
        consumed = rx_after - rx_before
        self.log.info(f"Drone {self.uav.id} consumed {consumed} of {self.frames_sent} MPC commands sent "
                      f"({100.0 * (1 - consumed / self.frames_sent):.1f}% lost).")

    async def _end_session(self) -> None:
        """Hands the drone back to the high-level commander, and executes the policy."""
        if self.policy == "none" or self.frames_received == 0:
            return
        try:
            # Tells the firmware that low-level setpoints stopped, so the high-level commander takes over from the
            # current state. Payload: meta command, then how long the last setpoint stays valid [ms].
            await self.cf.send_packet(port=CRTP_PORT_SETPOINT_GENERIC, channel=META_COMMAND_CHANNEL,
                                      data=struct.pack("<BI", META_NOTIFY_SETPOINTS_STOP, 0))
            if self.policy == "hover":
                self.log.info(f"Drone {self.uav.id}: hovering for {self.policy_arg:.1f} s, then landing.")
                await self.cf.high_level_commander.go_to(0.0, 0.0, 0.0, duration=1.0, relative=True)
                await trio.sleep(self.policy_arg)
                await self.uav.land()
            elif self.policy == "land":
                self.log.info(f"Drone {self.uav.id}: landing with {self.policy_arg:.2f} m/s.")
                await self.uav.land(velocity=self.policy_arg)
        except Exception as exc:
            self.log.warning(f"Drone {self.uav.id}: end of session policy {self.policy} failed: {exc!r}")
