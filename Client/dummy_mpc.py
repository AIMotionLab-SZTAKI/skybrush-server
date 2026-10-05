"""Stand-in for the MPC: an off-board geometric position controller that outputs thrust + body rates, streamed to
the drone through the "mpc" port of the Skybrush server. Used to validate the streaming chain before flying the real
MPC.

The drone must already be airborne (take off from Skybrush Live or with a DroneHandler), with the "Bodyrate"
controller selected and bodyrate.input_src = 1. The script takes the current position as its hover reference, so
there is no jump when it takes over. Mission: hover, step in x, step back, hover, then end the session, upon which
the server lands the drone.

The script aborts (ends the session, i.e. the server lands the drone) if mocap is lost, if the position error or
the tilt becomes too large, or on Ctrl+C.

    poetry run python Client/dummy_mpc.py 07 --profile cf                  # fly
    poetry run python Client/dummy_mpc.py 07 --profile cf --sim --no-server  # simulate only, no server, no mocap
    poetry run python Client/dummy_mpc.py 07 --profile cf --sim            # simulated drone, frames go to server

Mocap: the rigid body is looked up by name, which is the drone ID with the profile's prefix (e.g. "cf07", "bb07").
"""

import os
import sys
import threading
import time
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from mpc_client import MpcClient

G = 9.81


@dataclass
class Profile:
    mass: float          # kg
    max_thrust: float    # N
    kp: Tuple[float, float, float]  # position gains [1/s^2]
    kd: Tuple[float, float, float]  # velocity gains [1/s]
    k_att: float         # attitude error -> body rate gain [1/s]
    k_yaw: float         # yaw error -> yaw rate gain [1/s]
    max_rate: float      # deg/s, roll and pitch
    max_yaw_rate: float  # deg/s
    mocap_prefix: str


PROFILES = {
    "cf": Profile(mass=0.03, max_thrust=0.55, kp=(10.0, 10.0, 16.0), kd=(6.0, 6.0, 7.0), k_att=3.0, k_yaw=2.0,
                  max_rate=200.0, max_yaw_rate=90.0, mocap_prefix="cf"),
    "bb": Profile(mass=0.635, max_thrust=15.0, kp=(3.0, 3.0, 5.0), kd=(2.5, 2.5, 3.5), k_att=5.0, k_yaw=1.5,
                  max_rate=150.0, max_yaw_rate=60.0, mocap_prefix="bb"),
}

MAX_POSITION_ERROR = 0.5  # m
MAX_TILT = np.radians(45)
MOCAP_TIMEOUT = 0.1  # s


def quat_to_rot(w: float, x: float, y: float, z: float) -> np.ndarray:
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def vee(m: np.ndarray) -> np.ndarray:
    return np.array([m[2, 1], m[0, 2], m[1, 0]])


def hat(v: np.ndarray) -> np.ndarray:
    return np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])


def yaw_of(rot: np.ndarray) -> float:
    return float(np.arctan2(rot[1, 0], rot[0, 0]))


class Controller:
    """Geometric position controller with body rate output (Lee et al., with the attitude loop cut at the rates)."""

    def __init__(self, profile: Profile):
        self.p = profile

    def __call__(self, pos, vel, rot, pos_ref, yaw_ref) -> Tuple[float, np.ndarray]:
        """Returns thrust [N] and body rates (p, q, r) [rad/s] in the FLU body frame."""
        acc = -np.array(self.p.kp) * (pos - pos_ref) - np.array(self.p.kd) * vel + np.array([0.0, 0.0, G])
        # Keep some vertical acceleration, so that the desired attitude stays well defined
        acc[2] = max(acc[2], 0.3 * G)
        thrust = float(np.clip(self.p.mass * acc @ rot[:, 2], 0.0, self.p.max_thrust))
        b3 = acc / np.linalg.norm(acc)
        b1c = np.array([np.cos(yaw_ref), np.sin(yaw_ref), 0.0])
        b2 = np.cross(b3, b1c)
        b2 /= np.linalg.norm(b2)
        rot_d = np.column_stack((np.cross(b2, b3), b2, b3))
        e_rot = 0.5 * vee(rot_d.T @ rot - rot.T @ rot_d)
        rates = -np.array([self.p.k_att, self.p.k_att, self.p.k_yaw]) * e_rot
        max_rate = np.radians([self.p.max_rate, self.p.max_rate, self.p.max_yaw_rate])
        return thrust, np.clip(rates, -max_rate, max_rate)


def to_wire(rates: np.ndarray) -> Tuple[float, float, float]:
    """Body rates [rad/s] -> (roll_rate, pitch_rate, yaw_rate) in deg/s, in the firmware's convention."""
    p, q, r = np.degrees(rates)
    return float(p), float(-q), float(r)


def from_wire(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Inverse of to_wire, mirroring what the firmware does with the command."""
    return np.radians([roll, -pitch, yaw])


class MocapSource:
    """Reads the pose of one rigid body from OptiTrack in a background thread."""

    def __init__(self, body: str, host: str):
        import motioncapture  # imported here so that --sim works without it

        self.body = body
        self.mc = motioncapture.connect("optitrack", {"hostname": host})
        self.lock = threading.Lock()
        self.pose: Optional[Tuple[float, np.ndarray, np.ndarray]] = None  # (time, position, rotation)
        self.vel = np.zeros(3)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> bool:
        """Stops the reader thread and releases the connection. Must be called before the interpreter exits:
        destroying the connection while the thread is inside waitForNextFrame() aborts the process ("terminate
        called without an active exception"). Returns False if the thread is stuck (no frames are coming), in which
        case the connection cannot be released safely."""
        self._stop.set()
        self._thread.join(timeout=0.5)
        if self._thread.is_alive():
            return False
        del self.mc
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.mc.waitForNextFrame()
            obj = self.mc.rigidBodies.get(self.body)
            if obj is None or obj.rotation is None:
                continue
            t = time.perf_counter()
            pos = np.array(obj.position, dtype=float)
            rot = quat_to_rot(obj.rotation.w, obj.rotation.x, obj.rotation.y, obj.rotation.z)
            with self.lock:
                if self.pose is not None and t > self.pose[0]:
                    # Finite difference, low pass filtered
                    self.vel = 0.7 * self.vel + 0.3 * (pos - self.pose[1]) / (t - self.pose[0])
                self.pose = (t, pos, rot)

    def state(self):
        with self.lock:
            if self.pose is None or time.perf_counter() - self.pose[0] > MOCAP_TIMEOUT:
                return None
            return self.pose[1].copy(), self.vel.copy(), self.pose[2].copy()


class SimulatedDrone:
    """Point mass with perfect (first order) body rate tracking. Enough to check signs and gains."""

    def __init__(self, profile: Profile, pos: np.ndarray):
        self.p = profile
        self.pos = pos.copy()
        self.vel = np.zeros(3)
        self.rot = np.eye(3)
        self.rates = np.zeros(3)
        self.thrust = profile.mass * G

    def apply(self, thrust: float, roll: float, pitch: float, yaw: float, dt: float) -> None:
        self.thrust = thrust
        alpha = dt / (0.03 + dt)  # 30 ms rate loop time constant
        self.rates += alpha * (from_wire(roll, pitch, yaw) - self.rates)
        self.rot = self.rot @ (np.eye(3) + hat(self.rates) * dt)
        u, _, vt = np.linalg.svd(self.rot)  # re-orthonormalize
        self.rot = u @ vt
        self.vel += (self.thrust / self.p.mass * self.rot[:, 2] - np.array([0.0, 0.0, G])) * dt
        self.pos += self.vel * dt

    def state(self):
        return self.pos.copy(), self.vel.copy(), self.rot.copy()


def reference(t: float, start: np.ndarray, step: float) -> np.ndarray:
    """Hover 3 s, step +x, hold 5 s, step back, hold 4 s. Steps are smoothed with a 0.5 s time constant."""
    offset = 0.0
    for t_step, target in ((3.0, step), (8.0, 0.0)):
        if t > t_step:
            offset = target + (offset - target) * np.exp(-(t - t_step) / 0.5)
    return start + np.array([offset, 0.0, 0.0])


MISSION_DURATION = 12.0


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("drone_id")
    parser.add_argument("--profile", choices=PROFILES.keys(), default="cf")
    parser.add_argument("--rate", type=float, default=100.0, help="control rate [Hz]")
    parser.add_argument("--step", type=float, default=0.3, help="x step size [m]")
    parser.add_argument("--policy", default="land", choices=["land", "hover", "none"])
    parser.add_argument("--mocap-host", default="192.168.2.141")
    parser.add_argument("--sim", action="store_true", help="control a simulated drone instead of the mocap one")
    parser.add_argument("--no-server", action="store_true", help="with --sim: do not connect to the server")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    controller = Controller(profile)
    dt = 1.0 / args.rate

    if args.sim:
        source = SimulatedDrone(profile, np.array([0.0, 0.0, 0.5]))
    else:
        source = MocapSource(f"{profile.mocap_prefix}{args.drone_id}", args.mocap_host)
        t_wait = time.perf_counter()
        while source.state() is None:
            if time.perf_counter() - t_wait > 3.0:
                raise RuntimeError(f"No mocap data for rigid body {source.body}")
            time.sleep(0.01)

    pos, _, rot = source.state()
    start, yaw_ref = pos, yaw_of(rot)
    print(f"Hover reference: {np.round(start, 3)}, yaw {np.degrees(yaw_ref):.1f} deg")
    if not args.sim and start[2] < 0.2:
        raise RuntimeError("The drone does not seem to be airborne; take off from Skybrush first.")

    mpc = None if (args.sim and args.no_server) else MpcClient(args.drone_id, policy=args.policy)
    max_error = 0.0
    reason = "mission finished"
    try:
        t0 = time.perf_counter()
        next_t = t0
        while (t := time.perf_counter() - t0) < MISSION_DURATION:
            state = source.state()
            if state is None:
                reason = "mocap lost"
                break
            pos, vel, rot = state
            pos_ref = reference(t, start, args.step)
            error = float(np.linalg.norm(pos - pos_ref))
            max_error = max(max_error, error)
            tilt = float(np.arccos(np.clip(rot[2, 2], -1.0, 1.0)))
            if error > MAX_POSITION_ERROR or tilt > MAX_TILT:
                reason = f"safety limit (position error {error:.2f} m, tilt {np.degrees(tilt):.0f} deg)"
                break
            thrust, rates = controller(pos, vel, rot, pos_ref, yaw_ref)
            roll, pitch, yaw = to_wire(rates)
            if mpc is not None:
                mpc.send(thrust, roll, pitch, yaw)
            if args.sim:
                source.apply(thrust, roll, pitch, yaw, dt)
            if int(t / dt) % int(args.rate) == 0:
                print(f"t={t:5.2f} pos={np.round(pos, 3)} ref={np.round(pos_ref, 3)} thrust={thrust:.3f} N "
                      f"rates=({roll:6.1f}, {pitch:6.1f}, {yaw:6.1f}) deg/s")
            next_t += dt
            time.sleep(max(0.0, next_t - time.perf_counter()))
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        if mpc is not None:
            mpc.close()
    print(f"Session ended: {reason}. Max position error: {max_error:.3f} m. Server policy: {args.policy}.")
    if isinstance(source, MocapSource) and not source.close():
        # The reader thread is blocked in the C++ library, which would abort at interpreter exit. The session is
        # already closed, so skip the destructors.
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
