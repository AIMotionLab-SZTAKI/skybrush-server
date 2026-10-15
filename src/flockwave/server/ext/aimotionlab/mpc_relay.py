"""Relay that streams external (e.g. MPC) body rate + thrust commands from a TCP client to one drone, or to a group
of drones that must start and end together (e.g. a cooperatively carried payload).

Protocol, from the client's side:

1. Send ``REQ_<drone ids>[_<policy>[_<arg>]]`` as UTF-8 text, e.g. ``REQ_07``, ``REQ_07_land_0.3`` or
   ``REQ_07,08_hover_2.0``. ``<drone ids>`` is one drone ID, or several separated by commas (a group). The policy
   decides what happens when the session ends (see below):

   - ``land`` (default): land with the given descent velocity [m/s] (default 0.5).
   - ``hover``: hold the current position for the given number of seconds (default 2.0), then land.
   - ``none``: do nothing, the firmware's own timeout decides what happens.

2. Wait for ``ACK_<drone ids>``, or ``ACK_00`` if any of the drones is not available (all or nothing). Numeric IDs
   match regardless of leading zeros, and the ACK carries the IDs as the server knows them: ``REQ_8`` is answered
   with ``ACK_08``, ``REQ_7,8`` with ``ACK_07,08``.
3. Send binary frames, each of which is forwarded to the drone unchanged as a generic setpoint (CRTP port 7,
   channel 0). The first byte is the setpoint type, which also sets the frame size:
   - type 20, body rates + thrust: ``struct.pack("<BBffffff", 20, status, thrust, roll_rate, pitch_rate,
     yaw_rate, com_x, com_y)``: thrust in N, rates in deg/s with pitch inverted (legacy), center of mass shift
     in m. 26 bytes.
   - type 21, body rates + angular acceleration feedforward + thrust (for an onboard INDI):
     ``struct.pack("<BBfffffff", 21, status, thrust, wx, wy, wz, ax, ay, az)``: thrust in N, body rates in
     rad/s, angular accelerations in rad/s^2, all in the FLU body frame (no sign flips). 30 bytes, i.e. a full
     CRTP packet.
   For a group, each tick is one group frame: one frame per drone, concatenated in the order of the request (the
   types may differ between drones). A group frame is relayed only once it arrived completely, and its frames are
   sent to the drones together.
4. To end the session, either send the single byte ``0x00`` (in place of a frame, or of a group frame), or close
   the connection.

Onboard log (optional): append ``_log[<Hz>][:<profile>]`` to the request, e.g. ``REQ_07_land_0.3_log100`` or
``REQ_07_land_0.3_log50:indi`` (default 50 Hz, profile ``mpc``). During the session the server then streams the log
variables of the profile (``MPC_LOG_PROFILES``) of every drone back to the client as JSON lines, one per received
log packet: ``{"drone": "<id>", "block": "a", "t_fw": <firmware ms>, "t_srv": <server unix time>, "<variable>":
<value>, ...}``. If the variables cannot be logged on a drone (e.g. older firmware), a single ``{"drone": "<id>",
"error": "..."}`` line is sent and the session continues without that drone's log. Clients that do not ask for the
log receive nothing after the ACK.

The relay is latest-wins: if frames arrive faster than the radio can send them, only the newest (group) frame is
sent. The session ends on the end byte, a disconnect, no frame for longer than ``STREAM_TIMEOUT`` after the first
one, an invalid frame, or a radio error on any drone. A group always ends as a whole: the server hands every drone
back to the high-level commander, and executes the policy chosen in the handshake on all of them at once.
"""

import json
import struct
import time
from logging import Logger
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import trio
from aiocflib.crazyflie import Crazyflie

from flockwave.server.ext.crazyflie.driver import CrazyflieUAV

__all__ = ("MpcSession", "parse_mpc_request", "split_mpc_options", "split_uav_ids", "resolve_uav_id", "MPC_FRAME_SIZE",
           "MPC_FRAME_SIZES", "MPC_LOG_BLOCKS", "MPC_LOG_PROFILES")

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

RX_COUNTER_PARAMETERS = ("mpcin.rx", "bodyrate.mpc_rx")
"""Read-only firmware parameter counting the MPC commands the controller consumed; the first one the firmware has
(bodyrate.mpc_rx in firmware before the shared MPC input module)."""

STREAM_TIMEOUT = 0.25
"""If no frame arrives for this long (after the first one), the session is ended."""

MPC_LOG_PROFILES = {
    # Each block must fit into one log block of the firmware: LOG_MAX_LEN = 26 bytes in log.c (aiocflib only checks
    # 28). Block names are unique across profiles.
    "mpc": {
        # What the firmware received (bumblebee-firmware mpc_input.c): command echo and latency checks
        "a": ("ctrlMpc.type", "ctrlMpc.state", "ctrlMpc.thrust", "ctrlMpc.roll", "ctrlMpc.pitch", "ctrlMpc.yaw",
              "ctrlMpc.aff_x", "ctrlMpc.aff_y"),
        "b": ("ctrlMpc.aff_z", "gyro.x", "gyro.y", "gyro.z", "ctrlMpc.rx"),
    },
    "cf": {
        # Crazyflie firmware with the body rate controller (crazyflie-firmware controller_body_rate.c, type 20 only)
        "c1": ("ctrlMpc.state", "ctrlMpc.rx", "ctrlMpc.thrust", "ctrlMpc.roll", "ctrlMpc.pitch", "ctrlMpc.yaw"),
        "c2": ("gyro.x", "gyro.y", "gyro.z"),
    },
    "indi": {
        # The onboard INDI (controller_indi.c), for INDI flights
        # The commanded torque is not logged: it is G1 u of the logged rotor commands (after saturation)
        "i1": ("indi.mode", "ctrlMpc.state", "indi.rpm_inv", "indi.u1", "indi.u2", "indi.u3", "indi.u4", "indi.tf_z"),
        "i2": ("indi.wr_x", "indi.wr_y", "indi.wr_z", "indi.wf_x", "indi.wf_y", "indi.wf_z"),
        "i3": ("indi.a_x", "indi.a_y", "indi.a_z", "indi.tf_x", "indi.tf_y"),
    },
}
"""Log blocks streamed back to clients that ask for the onboard log. mpc: ctrlMpc.roll/pitch/yaw is the received
rate setpoint (deg/s, pitch inverted), aff_* the type 21 angular acceleration feedforward (rad/s^2, FLU), gyro.* in
deg/s. cf: the same for the Crazyflie body rate controller (thrust in the firmware's units). indi: Omega_ref after propagation (wr), filtered rate (wf), commanded angular acceleration (a), torque of the
measured rotor speeds (tf), rotor thrust commands u1..u4 [N], all SI and FLU."""

MPC_LOG_BLOCKS = MPC_LOG_PROFILES["mpc"]
"""The default profile."""

DEFAULT_LOG_PROFILE = "mpc"

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


def split_mpc_options(request: str) -> Tuple[str, Optional[float], Optional[str]]:
    """Strips the optional trailing ``_log[<Hz>][:<profile>]`` option from a request. Returns (request without the
    option, log rate in Hz or None, log profile or None)."""
    head, sep, last = request.strip().rpartition("_")
    if not sep or not last.startswith("log"):
        return request.strip(), None, None
    rate, _, profile = last[len("log"):].partition(":")
    try:
        hz = float(rate) if rate else DEFAULT_LOG_RATE
    except ValueError:
        raise ValueError(f"Malformed log option: {last!r}") from None
    if not 0 < hz <= 100:
        raise ValueError(f"Log rate must be in (0, 100] Hz: {last!r}")
    profile = profile or DEFAULT_LOG_PROFILE
    if profile not in MPC_LOG_PROFILES:
        raise ValueError(f"Unknown log profile {profile!r}, known: {sorted(MPC_LOG_PROFILES)}")
    return head, hz, profile


def split_uav_ids(uav_ids: str) -> List[str]:
    """Splits the ``<drone ids>`` field of a request, ``07`` or ``07,08``. Raises ValueError on empty or repeated
    IDs."""
    ids = uav_ids.split(",")
    if not all(ids):
        raise ValueError(f"Empty drone ID in {uav_ids!r}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"Repeated drone ID in {uav_ids!r}")
    return ids


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
    """One client streaming commands to one drone, or to a group of drones that start and end together."""

    def __init__(self, uavs: Union[CrazyflieUAV, Sequence[CrazyflieUAV]], stream: trio.SocketStream, log: Logger,
                 policy: str, policy_arg: Optional[float], log_hz: Optional[float] = None,
                 log_profile: str = DEFAULT_LOG_PROFILE):
        self.uavs = list(uavs) if isinstance(uavs, (list, tuple)) else [uavs]
        self.cfs: List[Crazyflie] = [uav._get_crazyflie() for uav in self.uavs]
        self.stream = stream
        self.log = log
        self.policy = policy
        self.policy_arg = policy_arg
        self._latest: Optional[Tuple[bytes, ...]] = None
        self._new_frame = trio.Event()
        self._send_error: Optional[str] = None
        self.frames_received = 0  # (group) frames
        self.frames_sent = 0  # (group) frames, each sent to every drone
        self.log_hz = log_hz
        self.log_profile = log_profile
        self.log_records_sent = 0
        self._log_sessions = []
        self._send_lock = trio.Lock()  # log records of several drones share the stream

    @property
    def name(self) -> str:
        return ("drone " if len(self.uavs) == 1 else "drones ") + ",".join(uav.id for uav in self.uavs)

    async def run(self) -> None:
        """Relays frames until the session ends, then executes the end of session policy on every drone."""
        reason = "unknown"
        rx_before = await self._for_all(self._read_rx_counter)
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._send_frames, nursery.cancel_scope)
                if self.log_hz:
                    for uav, cf in zip(self.uavs, self.cfs):
                        nursery.start_soon(self._stream_log, uav, cf)
                reason = await self._receive_frames()
                nursery.cancel_scope.cancel()
            if self._send_error is not None:
                reason = self._send_error
        finally:
            self.log.info(f"MPC session of {self.name} ended ({reason}), received {self.frames_received}, "
                          f"sent {self.frames_sent} frames. Policy: {self.policy}.")
            # Hand the drones back even if we are being cancelled, e.g. by server shutdown.
            with trio.CancelScope(shield=True):
                await self._end_session()
                rx_after = await self._for_all(self._read_rx_counter)
                self._report_rx_counters(rx_before, rx_after)
                # Only after the handover: removing the log blocks takes a few radio round trips
                await self._for_all(self._close_log_session)
                if self.log_records_sent:
                    self.log.info(f"MPC session of {self.name}: streamed {self.log_records_sent} onboard log records.")

    async def _for_all(self, func, *args) -> list:
        """Runs func(uav, cf, *args) for every drone concurrently; returns the results in drone order."""
        results = [None] * len(self.uavs)

        async def one(k):
            results[k] = await func(self.uavs[k], self.cfs[k], *args)

        async with trio.open_nursery() as nursery:
            for k in range(len(self.uavs)):
                nursery.start_soon(one, k)
        return results

    async def _receive_frames(self) -> str:
        """Reads (group) frames into the latest-wins slot. Returns the reason why the session ended."""
        buffer = bytearray()
        n = len(self.uavs)
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
                    frames, offset = [], 0
                    while len(frames) < n and offset < len(buffer):
                        size = MPC_FRAME_SIZES.get(buffer[offset])
                        if size is None:
                            return f"invalid frame type {buffer[offset]}"
                        if len(buffer) < offset + size:
                            break
                        frames.append(bytes(buffer[offset:offset + size]))
                        offset += size
                    if len(frames) < n:
                        break  # wait for the rest of the (group) frame
                    self._latest = tuple(frames)
                    del buffer[:offset]
                    self.frames_received += 1
                    self._new_frame.set()
                continue
            return f"no frame for {STREAM_TIMEOUT * 1000:.0f} ms"

    async def _send_frames(self, cancel_scope: trio.CancelScope) -> None:
        """Sends the newest (group) frame whenever there is one, to all drones at once; the next one only after all
        of them went out. Frames overwritten while the radio was busy are dropped. A radio error ends the session."""
        while True:
            await self._new_frame.wait()
            self._new_frame = trio.Event()
            frames = self._latest
            try:
                await self._for_all(self._send_setpoint, frames)
            except Exception as exc:
                while getattr(exc, "exceptions", None):  # exception group from the nursery in _for_all
                    exc = exc.exceptions[0]
                self._send_error = f"radio error, {exc}"
                cancel_scope.cancel()
                return
            self.frames_sent += 1

    async def _send_setpoint(self, uav: CrazyflieUAV, cf: Crazyflie, frames: Tuple[bytes, ...]) -> None:
        try:
            await cf.send_packet(port=CRTP_PORT_SETPOINT_GENERIC, channel=SET_SETPOINT_CHANNEL,
                                 data=frames[self.uavs.index(uav)])
        except Exception as exc:
            raise RuntimeError(f"drone {uav.id}: {exc!r}") from exc

    async def _send_line(self, record: dict) -> None:
        async with self._send_lock:
            await self.stream.send_all((json.dumps(record) + "\n").encode("utf-8"))

    async def _stream_log(self, uav: CrazyflieUAV, cf: Crazyflie) -> None:
        """Streams the onboard MPC log variables of one drone to the client as JSON lines. Never raises: logging
        problems must not end the MPC session. The log blocks stay on the drone until _close_log_session()."""
        try:
            await cf.log.validate()  # make sure the log TOC is loaded
            session = cf.log.create_session()
            session.configure(graceful_cleanup=True)
            names = {}
            for name, variables in MPC_LOG_PROFILES[self.log_profile].items():
                names[id(session.create_block(*variables, frequency=self.log_hz))] = name
            self._log_sessions.append((uav, session))
            await session.__aenter__()
            async for message in session.messages():
                record = {"drone": uav.id, "block": names.get(id(message.block), "?"), "t_fw": message.timestamp,
                          "t_srv": time.time(), **message.to_dict()}
                await self._send_line(record)
                self.log_records_sent += 1
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            pass  # client gone; the receive side ends the session
        except Exception as exc:
            self.log.warning(f"Drone {uav.id}: MPC onboard log not available: {exc!r}")
            try:
                await self._send_line({"drone": uav.id, "error": repr(exc)})
            except Exception:
                pass

    async def _close_log_session(self, uav: CrazyflieUAV, cf: Crazyflie) -> None:
        """Removes the log blocks from the drone (best effort, bounded in time)."""
        for owner, session in [entry for entry in self._log_sessions if entry[0] is uav]:
            self._log_sessions.remove((owner, session))
            with trio.move_on_after(1.0):
                try:
                    await session.__aexit__(None, None, None)
                except Exception as exc:
                    self.log.warning(f"Drone {uav.id}: removing the MPC log blocks failed: {exc!r}")

    async def _read_rx_counter(self, uav: CrazyflieUAV, cf: Crazyflie) -> Optional[int]:
        """Reads how many MPC commands the firmware has consumed so far, or None if it cannot tell."""
        with trio.move_on_after(0.5):
            for name in RX_COUNTER_PARAMETERS:
                try:
                    return int(await uav.get_parameter(name, fetch=True))
                except Exception:
                    continue
        return None

    def _report_rx_counters(self, rx_before: list, rx_after: list) -> None:
        """Logs how many of the frames sent over the radio each firmware actually consumed."""
        if self.frames_sent == 0:
            return
        for uav, before, after in zip(self.uavs, rx_before, rx_after):
            if before is None or after is None:
                continue
            consumed = after - before
            self.log.info(f"Drone {uav.id} consumed {consumed} of {self.frames_sent} MPC commands sent "
                          f"({100.0 * (1 - consumed / self.frames_sent):.1f}% lost).")

    async def _end_session(self) -> None:
        """Hands every drone back to the high-level commander first, then executes the policy on all of them."""
        if self.policy == "none" or self.frames_received == 0:
            return
        # Tells the firmware that low-level setpoints stopped, so the high-level commander takes over from the
        # current state. Payload: meta command, then how long the last setpoint stays valid [ms].
        handed_over = await self._for_all(self._notify_setpoints_stop)
        await self._for_all(self._execute_policy, handed_over)

    async def _notify_setpoints_stop(self, uav: CrazyflieUAV, cf: Crazyflie) -> bool:
        try:
            await cf.send_packet(port=CRTP_PORT_SETPOINT_GENERIC, channel=META_COMMAND_CHANNEL,
                                 data=struct.pack("<BI", META_NOTIFY_SETPOINTS_STOP, 0))
            return True
        except Exception as exc:
            self.log.warning(f"Drone {uav.id}: handing back to the high-level commander failed: {exc!r}")
            return False

    async def _execute_policy(self, uav: CrazyflieUAV, cf: Crazyflie, handed_over: list) -> None:
        if not handed_over[self.uavs.index(uav)]:
            return
        try:
            if self.policy == "hover":
                self.log.info(f"Drone {uav.id}: hovering for {self.policy_arg:.1f} s, then landing.")
                await cf.high_level_commander.go_to(0.0, 0.0, 0.0, duration=1.0, relative=True)
                await trio.sleep(self.policy_arg)
                await uav.land()
            elif self.policy == "land":
                self.log.info(f"Drone {uav.id}: landing with {self.policy_arg:.2f} m/s.")
                await uav.land(velocity=self.policy_arg)
        except Exception as exc:
            self.log.warning(f"Drone {uav.id}: end of session policy {self.policy} failed: {exc!r}")
