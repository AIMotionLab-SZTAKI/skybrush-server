"""Client for the "mpc" port of the aimotionlab extension: streams thrust + body rate commands to one drone.

Usage from your own MPC code:

    with MpcClient("07", policy="land", policy_arg=0.3) as mpc:
        while running:
            mpc.send(thrust_N, roll_rate, pitch_rate, yaw_rate)   # deg/s, see the sign convention below
    # leaving the block ends the session: the server hands the drone back to the high-level commander and lands

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
import time
from argparse import ArgumentParser
from math import sin, pi, radians
from typing import Optional

MPC_SETPOINT_TYPE = 20
MPC_INDI_SETPOINT_TYPE = 21
MPC_END_SESSION = b"\x00"
FRAME = struct.Struct("<BBffffff")
INDI_FRAME = struct.Struct("<BBfffffff")  # 30 bytes, a full CRTP packet


class MpcClient:
    def __init__(self, drone_id: str, policy: str = "land", policy_arg: Optional[float] = None,
                 host: str = "127.0.0.1", port: int = 6004):
        """policy: what the server does when the session ends: "land" (arg: descent velocity [m/s]), "hover"
        (arg: hover time [s] before landing), or "none" (leave it to the firmware)."""
        self.drone_id = drone_id
        request = f"REQ_{drone_id}_{policy}" + (f"_{policy_arg}" if policy_arg is not None else "")
        self.sock = socket.create_connection((host, port))
        # Without this, the kernel may hold back small frames to batch them together (Nagle's algorithm).
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(request.encode("utf-8"))
        ack = self.sock.recv(64).decode("utf-8", errors="replace")
        if not ack.startswith("ACK_") or ack == "ACK_00":
            self.sock.close()
            raise RuntimeError(f"Server refused MPC request {request!r} (answer: {ack!r}), see the server log")
        self.drone_id = ack[len("ACK_"):]  # as the server knows it, e.g. "08" for "8"
        self.closed = False

    def send(self, thrust: float, roll_rate: float, pitch_rate: float, yaw_rate: float, status: int = 0,
             com_x: float = 0.0, com_y: float = 0.0) -> None:
        """Sends one command. thrust in N, rates in deg/s, center of mass shift in m. A nonzero status marks the
        command as invalid; the bumblebee firmware stops the motors after too many invalid commands in a row."""
        self.sock.sendall(FRAME.pack(MPC_SETPOINT_TYPE, status, thrust, roll_rate, pitch_rate, yaw_rate,
                                     com_x, com_y))

    def send_indi(self, thrust: float, omega, alpha, status: int = 0) -> None:
        """Sends one type 21 command: thrust in N, body rates omega (3) in rad/s and angular acceleration
        feedforward alpha (3) in rad/s^2, FLU body frame. A nonzero status marks the command as invalid."""
        self.sock.sendall(INDI_FRAME.pack(MPC_INDI_SETPOINT_TYPE, status, thrust, *omega, *alpha))

    def close(self) -> None:
        """Ends the session cleanly: the server executes the policy chosen when connecting."""
        if not self.closed:
            self.closed = True
            try:
                self.sock.sendall(MPC_END_SESSION)
            finally:
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
