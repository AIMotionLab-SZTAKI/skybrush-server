"""Client for the "mpc" port of the aimotionlab extension: streams thrust + body rate commands to one drone, or to a
group of drones that start and end together.

Usage from your own MPC code:

    with MpcClient("07", policy="land", policy_arg=0.3) as mpc:
        while running:
            mpc.send(thrust_N, roll_rate, pitch_rate, yaw_rate)   # deg/s, see the sign convention below
    # leaving the block ends the session: the server hands the drone back to the high-level commander and lands

A group gets one frame per drone and tick, in the order of the IDs; if anything goes wrong (the client stops
streaming or disconnects, a radio error on any drone), the server ends the session for every drone of the group:

    with MpcClient(["07", "08"], policy="land", policy_arg=0.3) as mpc:
        while running:
            mpc.send_frames([MpcClient.frame(T1, r1, p1, y1), MpcClient.frame(T2, r2, p2, y2)])

Sign convention (same as the UART path of the bumblebee body rate controller): with body rates (p, q, r) in the
drone's FLU body frame, send roll_rate = p, pitch_rate = -q, yaw_rate = r, all in deg/s.

For an onboard INDI, send_indi() streams type 21 instead: thrust [N], body rates [rad/s] and angular acceleration
feedforward [rad/s^2], all in the FLU body frame without sign flips.

Run as a script for a props-off bench test, which streams a slow sinusoid on the rates at a low thrust:

    poetry run python Client/mpc_client.py 07 --rate 100 --thrust 0.05 --duration 10
    poetry run python Client/mpc_client.py 07 --indi   # type 21; alpha_ff = (1, 2, 3) * sinusoid [rad/s^2]
"""

import socket
import struct
import threading
import time
from argparse import ArgumentParser
from math import sin, pi, radians
from typing import Optional, Sequence, Union

MPC_SETPOINT_TYPE = 20
MPC_INDI_SETPOINT_TYPE = 21
MPC_END_SESSION = b"\x00"
FRAME = struct.Struct("<BBffffff")
INDI_FRAME = struct.Struct("<BBfffffff")  # 30 bytes, a full CRTP packet


class MpcClient:
    def __init__(self, drone_ids: Union[str, Sequence[str]], policy: str = "land", policy_arg: Optional[float] = None,
                 host: str = "127.0.0.1", port: int = 6004, log_hz: Optional[float] = None,
                 log_profile: Optional[str] = None, log_path: Optional[str] = None):
        """drone_ids: one drone ID, or a list of them for a group session.
        policy: what the server does when the session ends: "land" (arg: descent velocity [m/s]), "hover"
        (arg: hover time [s] before landing), or "none" (leave it to the firmware).
        log_hz, log_profile: ask for the onboard log (see mpc_relay.py); its JSON lines are written to log_path."""
        ids = [drone_ids] if isinstance(drone_ids, str) else list(drone_ids)
        request = f"REQ_{','.join(ids)}_{policy}" + (f"_{policy_arg}" if policy_arg is not None else "")
        if log_hz:
            request += f"_log{log_hz:g}" + (f":{log_profile}" if log_profile else "")
        self.sock = socket.create_connection((host, port))
        # Without this, the kernel may hold back small frames to batch them together (Nagle's algorithm).
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(request.encode("utf-8"))
        # The ACK has no terminator; log lines (JSON objects) may follow it in the same read
        ack, brace, rest = self.sock.recv(256).decode("utf-8", errors="replace").partition("{")
        if not ack.startswith("ACK_") or ack == "ACK_00":
            self.sock.close()
            raise RuntimeError(f"Server refused MPC request {request!r} (answer: {ack!r}), see the server log")
        self.drone_ids = ack[len("ACK_"):].split(",")  # as the server knows them, e.g. "08" for "8"
        self.drone_id = ",".join(self.drone_ids)
        self.closed = False
        self.log_records = 0
        self._log_thread = None
        if log_hz:
            self._log_thread = threading.Thread(target=self._read_log, args=(log_path, (brace + rest).encode()),
                                                daemon=True)
            self._log_thread.start()

    @staticmethod
    def frame(thrust: float, roll_rate: float, pitch_rate: float, yaw_rate: float, status: int = 0,
              com_x: float = 0.0, com_y: float = 0.0) -> bytes:
        """One type 20 frame, see send()."""
        return FRAME.pack(MPC_SETPOINT_TYPE, status, thrust, roll_rate, pitch_rate, yaw_rate, com_x, com_y)

    @staticmethod
    def indi_frame(thrust: float, omega, alpha, status: int = 0) -> bytes:
        """One type 21 frame, see send_indi()."""
        return INDI_FRAME.pack(MPC_INDI_SETPOINT_TYPE, status, thrust, *omega, *alpha)

    def send_frames(self, frames: Sequence[bytes]) -> None:
        """Sends one tick: one frame (frame() or indi_frame()) per drone, in the order of the drone IDs."""
        if len(frames) != len(self.drone_ids):
            raise ValueError(f"{len(frames)} frames for {len(self.drone_ids)} drones")
        self.sock.sendall(b"".join(frames))

    def _read_log(self, path: Optional[str], pending: bytes) -> None:
        """Writes the onboard log lines to path (JSON lines) until the connection closes."""
        out = open(path, "w") if path else None
        try:
            while True:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.log_records += 1
                    if out:
                        out.write(line.decode("utf-8", errors="replace") + "\n")
                try:
                    data = self.sock.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                pending += data
        finally:
            if out:
                out.close()

    def send(self, thrust: float, roll_rate: float, pitch_rate: float, yaw_rate: float, status: int = 0,
             com_x: float = 0.0, com_y: float = 0.0) -> None:
        """Sends one command to a single drone. thrust in N, rates in deg/s, center of mass shift in m. A nonzero
        status marks the command as invalid; the bumblebee firmware stops the motors after too many invalid commands
        in a row."""
        self.send_frames([self.frame(thrust, roll_rate, pitch_rate, yaw_rate, status, com_x, com_y)])

    def send_indi(self, thrust: float, omega, alpha, status: int = 0) -> None:
        """Sends one type 21 command: thrust in N, body rates omega (3) in rad/s and angular acceleration
        feedforward alpha (3) in rad/s^2, FLU body frame. A nonzero status marks the command as invalid."""
        self.send_frames([self.indi_frame(thrust, omega, alpha, status)])

    def close(self) -> None:
        """Ends the session cleanly: the server executes the policy chosen when connecting."""
        if not self.closed:
            self.closed = True
            try:
                self.sock.sendall(MPC_END_SESSION)
            except OSError:
                pass  # the server already ended the session
            finally:
                if self._log_thread is not None:
                    # The server stops logging when the session ends; read what is still in flight, then wake the
                    # reader thread up (shutdown, unlike close, interrupts a blocking recv)
                    self._log_thread.join(timeout=0.5)
                    try:
                        self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self._log_thread.join(timeout=1.0)
                self.sock.close()

    def __enter__(self) -> "MpcClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def main() -> None:
    parser = ArgumentParser(description="Props-off bench test: streams a sinusoid on the body rates.")
    parser.add_argument("drone_id")
    parser.add_argument("--rate", type=float, default=100.0, help="streaming rate [Hz]")
    parser.add_argument("--thrust", type=float, default=0.05, help="constant thrust [N]")
    parser.add_argument("--amplitude", type=float, default=30.0, help="rate amplitude [deg/s]")
    parser.add_argument("--period", type=float, default=2.0, help="sinusoid period [s]")
    parser.add_argument("--duration", type=float, default=10.0, help="test duration [s]")
    parser.add_argument("--policy", default="none", choices=["land", "hover", "none"])
    parser.add_argument("--indi", action="store_true",
                        help="send type 21 (rates in rad/s, FLU) with alpha_ff = (1, 2, 3) * the sinusoid")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6004)
    args = parser.parse_args()

    dt = 1.0 / args.rate
    with MpcClient(args.drone_id, policy=args.policy, host=args.host, port=args.port) as mpc:
        print(f"Streaming to drone {args.drone_id} at {args.rate:.0f} Hz for {args.duration:.1f} s")
        t0 = time.perf_counter()
        next_t = t0
        sent = 0
        while (t := time.perf_counter() - t0) < args.duration:
            w = args.amplitude * sin(2 * pi * t / args.period)
            # roll, pitch and yaw in phase shifted sinusoids so that each axis can be told apart in the logs
            w_pitch = args.amplitude * sin(2 * pi * t / args.period + pi / 2)
            if args.indi:
                s = sin(2 * pi * t / args.period)
                mpc.send_indi(args.thrust, (radians(w), radians(w_pitch), radians(0.5 * w)), (s, 2 * s, 3 * s))
            else:
                mpc.send(args.thrust, w, w_pitch, 0.5 * w)
            sent += 1
            next_t += dt
            time.sleep(max(0.0, next_t - time.perf_counter()))
    print(f"Sent {sent} frames, {sent / args.duration:.1f} Hz on average.")


if __name__ == "__main__":
    main()
