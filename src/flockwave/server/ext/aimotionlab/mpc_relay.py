"""Relay that streams external (e.g. MPC) body rate + thrust commands from a TCP client to one drone.

Protocol, from the client's side:

1. Send ``REQ_<drone id>[_<policy>[_<arg>]]`` as UTF-8 text, e.g. ``REQ_07``, ``REQ_07_land_0.3`` or
   ``REQ_07_hover_2.0``. The policy decides what happens when the session ends (see below):

   - ``land`` (default): land with the given descent velocity [m/s] (default 0.5).
   - ``hover``: hold the current position for the given number of seconds (default 2.0), then land.
   - ``none``: do nothing, the firmware's own timeout decides what happens.

2. Wait for ``ACK_<drone id>``, or ``ACK_00`` if the drone is not available. Numeric IDs match regardless of
   leading zeros, and the ACK carries the ID as the server knows it: ``REQ_8`` is answered with ``ACK_08``.
3. Send binary frames, each of which is forwarded to the drone unchanged as a generic setpoint (CRTP port 7,
   channel 0). The first byte is the setpoint type, which also sets the frame size:
   - type 20, body rates + thrust: ``struct.pack("<BBffffff", 20, status, thrust, roll_rate, pitch_rate,
     yaw_rate, com_x, com_y)``: thrust in N, rates in deg/s with pitch inverted (legacy), center of mass shift
     in m. 26 bytes.
   - type 21, body rates + angular acceleration feedforward + thrust (for an onboard INDI):
     ``struct.pack("<BBfffffff", 21, status, thrust, wx, wy, wz, ax, ay, az)``: thrust in N, body rates in
     rad/s, angular accelerations in rad/s^2, all in the FLU body frame (no sign flips). 30 bytes, i.e. a full
     CRTP packet.
4. To end the session, either send the single byte ``0x00``, or close the connection.

Onboard log (optional): append ``_log`` (50 Hz) or ``_log<Hz>`` to the request, e.g. ``REQ_07_land_0.3_log100``.
During the session the server then streams the MPC log variables of the firmware (``MPC_LOG_BLOCKS``) back to the
client as JSON lines, one per received log packet: ``{"block": "a", "t_fw": <firmware ms>, "t_srv": <server unix
time>, "<variable>": <value>, ...}``. If the variables cannot be logged (e.g. older firmware), a single
``{"error": "..."}`` line is sent and the session continues without logging. Clients that do not ask for the log
receive nothing after the ACK.

The relay is latest-wins: if frames arrive faster than the radio can send them, only the newest one is sent. When
the session ends (end byte, disconnect, or no frame for longer than ``STREAM_TIMEOUT`` after the first frame), the
server hands the drone back to the high-level commander, and executes the policy chosen in the handshake.
"""

import json
import struct
import time
from logging import Logger
from typing import Iterable, Optional, Tuple

import trio
from aiocflib.crazyflie import Crazyflie

from flockwave.server.ext.crazyflie.driver import CrazyflieUAV

__all__ = ("MpcSession", "parse_mpc_request", "split_mpc_options", "resolve_uav_id", "MPC_FRAME_SIZE",
           "MPC_FRAME_SIZES", "MPC_LOG_BLOCKS")

MPC_SETPOINT_TYPE = 20
"""Generic setpoint type of the body rate MPC command, see crtp_commander_generic.c in the firmware."""

MPC_INDI_SETPOINT_TYPE = 21
"""Generic setpoint type of the MPC command with angular acceleration feedforward (onboard INDI)."""

MPC_END_SESSION = 0x00
"""Single byte a client may send to end the session cleanly."""

MPC_FRAME_SIZE = struct.calcsize("<BBffffff")  # 26 bytes, fits into one CRTP packet (max 30)

MPC_FRAME_SIZES = {
    MPC_SETPOINT_TYPE: MPC_FRAME_SIZE,
    MPC_INDI_SETPOINT_TYPE: struct.calcsize("<BBfffffff"),  # 30 bytes, a full CRTP packet
}
"""Frame size of each accepted setpoint type, including the type byte."""

CRTP_PORT_SETPOINT_GENERIC = 7
SET_SETPOINT_CHANNEL = 0
META_COMMAND_CHANNEL = 1
META_NOTIFY_SETPOINTS_STOP = 0

RX_COUNTER_PARAMETER = "bodyrate.mpc_rx"
"""Read-only firmware parameter counting the MPC commands the controller consumed."""

STREAM_TIMEOUT = 0.25
"""If no frame arrives for this long (after the first one), the session is ended."""

MPC_LOG_BLOCKS = {
    # Each block must fit into one log packet (28 bytes)
    "a": ("ctrlMpc.type", "ctrlMpc.state", "ctrlMpc.thrust", "ctrlMpc.roll", "ctrlMpc.pitch", "ctrlMpc.yaw",
          "ctrlMpc.aff_x", "ctrlMpc.aff_y"),
    "b": ("ctrlMpc.aff_z", "gyro.x", "gyro.y", "gyro.z", "ctrlMpc.rx"),
}
"""Log blocks streamed back to clients that ask for the onboard log (see bumblebee-firmware controller_body_rate.c).
ctrlMpc.roll/pitch/yaw is the rate setpoint in use (deg/s, pitch inverted), aff_* the type 21 angular acceleration
feedforward (rad/s^2, FLU), gyro.* in deg/s."""

DEFAULT_LOG_RATE = 50.0
"""Onboard log rate [Hz] for a plain ``_log`` request option."""

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


def split_mpc_options(request: str) -> Tuple[str, Optional[float]]:
    """Strips the optional trailing ``_log`` / ``_log<Hz>`` option from a request. Returns (request without the
    option, log rate in Hz or None)."""
    head, sep, last = request.strip().rpartition("_")
    if not sep or not last.startswith("log"):
        return request.strip(), None
    rate = last[len("log"):]
    try:
        hz = float(rate) if rate else DEFAULT_LOG_RATE
    except ValueError:
        raise ValueError(f"Malformed log option: {last!r}") from None
    if not 0 < hz <= 100:
        raise ValueError(f"Log rate must be in (0, 100] Hz: {last!r}")
    return head, hz


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
                 policy_arg: Optional[float], log_hz: Optional[float] = None):
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
        self.log_hz = log_hz
        self.log_records_sent = 0
        self._log_session = None

    async def run(self) -> None:
        """Relays frames until the session ends, then executes the end of session policy."""
        reason = "unknown"
        rx_before = await self._read_rx_counter()
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._send_frames)
                if self.log_hz:
                    nursery.start_soon(self._stream_log)
                reason = await self._receive_frames()
                nursery.cancel_scope.cancel()
        finally:
            self.log.info(f"MPC session of drone {self.uav.id} ended ({reason}), received {self.frames_received}, "
                          f"sent {self.frames_sent} frames. Policy: {self.policy}.")
            # Hand the drone back even if we are being cancelled, e.g. by server shutdown.
            with trio.CancelScope(shield=True):
                await self._end_session()
                await self._report_rx_counter(rx_before)
                # Only after the handover: removing the log blocks takes a few radio round trips
                await self._close_log_session()

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
                    size = MPC_FRAME_SIZES.get(buffer[0])
                    if size is None:
                        return f"invalid frame type {buffer[0]}"
                    if len(buffer) < size:
                        break  # wait for the rest of the frame
                    self._latest = bytes(buffer[:size])
                    del buffer[:size]
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

    async def _stream_log(self) -> None:
        """Streams the onboard MPC log variables to the client as JSON lines. Never raises: logging problems must
        not end the MPC session. The log blocks stay on the drone until _close_log_session()."""
        try:
            await self.cf.log.validate()  # make sure the log TOC is loaded
            session = self.cf.log.create_session()
            session.configure(graceful_cleanup=True)
            names = {}
            for name, variables in MPC_LOG_BLOCKS.items():
                names[id(session.create_block(*variables, frequency=self.log_hz))] = name
            self._log_session = session
            await session.__aenter__()
            async for message in session.messages():
                record = {"block": names.get(id(message.block), "?"), "t_fw": message.timestamp,
                          "t_srv": time.time(), **message.to_dict()}
                await self.stream.send_all((json.dumps(record) + "\n").encode("utf-8"))
                self.log_records_sent += 1
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            pass  # client gone; the receive side ends the session
        except Exception as exc:
            self.log.warning(f"Drone {self.uav.id}: MPC onboard log not available: {exc!r}")
            try:
                await self.stream.send_all((json.dumps({"error": repr(exc)}) + "\n").encode("utf-8"))
            except Exception:
                pass

    async def _close_log_session(self) -> None:
        """Removes the log blocks from the drone (best effort, bounded in time)."""
        if self._log_session is None:
            return
        session, self._log_session = self._log_session, None
        with trio.move_on_after(1.0):
            try:
                await session.__aexit__(None, None, None)
            except Exception as exc:
                self.log.warning(f"Drone {self.uav.id}: removing the MPC log blocks failed: {exc!r}")
        if self.log_records_sent:
            self.log.info(f"Drone {self.uav.id}: streamed {self.log_records_sent} onboard log records.")

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
