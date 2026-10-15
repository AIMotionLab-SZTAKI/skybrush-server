"""Stand-in for the MPC: an off-board geometric position controller per drone that outputs thrust + body rates,
streamed to one or more drones through the "mpc" port of the Skybrush server. Used to validate the streaming chain
before flying the real MPC.

The drones must already be airborne (take off from Skybrush Live or with a DroneHandler), with the "Bodyrate"
controller selected and bodyrate.input_src = 1. The script takes each drone's current position as its hover
reference, so there is no jump when it takes over. Mission, for all drones together: hover, step in x, step back,
hover, then end the session, upon which the server lands the drones.

With several drones, the commands go out as one group session (one connection, the drones start and end together),
or with --per-drone as one session per drone (one connection each, for comparison). The script aborts (ends the
session(s), i.e. the server lands every drone) if mocap of any drone is lost, if any position error or tilt becomes
too large, if two drones get too close, or on Ctrl+C.

    poetry run python Client/dummy_mpc.py 07 --profile cf                  # fly
    poetry run python Client/dummy_mpc.py 07 08 --profile cf --log 100     # fly two, group session, onboard log
    poetry run python Client/dummy_mpc.py 07 08 --profile cf --per-drone   # fly two, one session per drone
    poetry run python Client/dummy_mpc.py 07 --profile cf --sim --no-server  # simulate only, no server, no mocap
    poetry run python Client/dummy_mpc.py 07 08 --profile cf --sim         # simulated drones, frames go to server

Mocap: each rigid body is looked up by name, which is the drone ID with the profile's prefix (e.g. "cf07", "bb07").
--log writes the onboard log (profile = --profile) as JSON lines to mpc_log_<ids>_<time>.jsonl, see mpc_relay.py.
"""

import os
import sys
import threading
import time
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
MIN_DISTANCE = 0.25  # m, between any two drones
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
    """Reads the poses of rigid bodies from OptiTrack in a background thread (one connection for all of them)."""

    def __init__(self, bodies: List[str], host: str):
        import motioncapture  # imported here so that --sim works without it

        self.bodies = bodies
        self.mc = motioncapture.connect("optitrack", {"hostname": host})
        self.lock = threading.Lock()
        self.poses: Dict[str, Tuple[float, np.ndarray, np.ndarray]] = {}  # body -> (time, position, rotation)
        self.vels = {body: np.zeros(3) for body in bodies}
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
            t = time.perf_counter()
            for body in self.bodies:
                obj = self.mc.rigidBodies.get(body)
                if obj is None or obj.rotation is None:
                    continue
                pos = np.array(obj.position, dtype=float)
                rot = quat_to_rot(obj.rotation.w, obj.rotation.x, obj.rotation.y, obj.rotation.z)
                with self.lock:
                    last = self.poses.get(body)
                    if last is not None and t > last[0]:
                        # Finite difference, low pass filtered
                        self.vels[body] = 0.7 * self.vels[body] + 0.3 * (pos - last[1]) / (t - last[0])
                    self.poses[body] = (t, pos, rot)

    def state(self, body: str):
        with self.lock:
            pose = self.poses.get(body)
            if pose is None or time.perf_counter() - pose[0] > MOCAP_TIMEOUT:
                return None
            return pose[1].copy(), self.vels[body].copy(), pose[2].copy()


class MocapDrone:
    """One rigid body of a MocapSource, with the interface of SimulatedDrone."""

    def __init__(self, source: MocapSource, body: str):
        self.source, self.body = source, body

    def state(self):
        return self.source.state(self.body)


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


def open_sessions(args, ids: List[str]) -> List[MpcClient]:
    """One group session for all drones, or with --per-drone one session per drone."""
    groups = [[i] for i in ids] if args.per_drone else [ids]
    clients = []
    try:
        for group in groups:
            log_path = None
            if args.log:
                log_path = f"mpc_log_{'_'.join(group)}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
            clients.append(MpcClient(group, policy=args.policy, host=args.host, port=args.port, log_hz=args.log,
                                     log_profile=args.profile, log_path=log_path))
    except Exception:
        for client in clients:  # all or nothing on the client side too
            client.close()
        raise
    return clients


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("drone_ids", nargs="+")
    parser.add_argument("--profile", choices=PROFILES.keys(), default="cf")
    parser.add_argument("--rate", type=float, default=100.0, help="control rate [Hz]")
    parser.add_argument("--step", type=float, default=0.3, help="x step size [m]")
    parser.add_argument("--policy", default="land", choices=["land", "hover", "none"])
    parser.add_argument("--per-drone", action="store_true", help="one session (connection) per drone, not a group")
    parser.add_argument("--log", type=float, nargs="?", const=100.0, default=None, metavar="HZ",
                        help="stream the onboard log (default 100 Hz) into mpc_log_*.jsonl")
    parser.add_argument("--host", default="127.0.0.1", help="Skybrush server")
    parser.add_argument("--port", type=int, default=6004, help="mpc port of the Skybrush server")
    parser.add_argument("--mocap-host", default="192.168.2.141")
    parser.add_argument("--sim", action="store_true", help="control simulated drones instead of the mocap ones")
    parser.add_argument("--no-server", action="store_true", help="with --sim: do not connect to the server")
    args = parser.parse_args()

    profile = PROFILES[args.profile]
    ids = args.drone_ids
    controllers = [Controller(profile) for _ in ids]
    dt = 1.0 / args.rate

    mocap = None
    if args.sim:
        drones = [SimulatedDrone(profile, np.array([0.0, 0.6 * k, 0.5])) for k in range(len(ids))]
    else:
        mocap = MocapSource([f"{profile.mocap_prefix}{i}" for i in ids], args.mocap_host)
        drones = [MocapDrone(mocap, body) for body in mocap.bodies]
        t_wait = time.perf_counter()
        while any(d.state() is None for d in drones):
            if time.perf_counter() - t_wait > 3.0:
                missing = [d.body for d in drones if d.state() is None]
                mocap.close()
                raise RuntimeError(f"No mocap data for rigid body(s) {missing}")
            time.sleep(0.01)

    starts, yaw_refs = [], []
    for drone_id, drone in zip(ids, drones):
        pos, _, rot = drone.state()
        starts.append(pos)
        yaw_refs.append(yaw_of(rot))
        print(f"Drone {drone_id}: hover reference {np.round(pos, 3)}, yaw {np.degrees(yaw_refs[-1]):.1f} deg")
        if not args.sim and pos[2] < 0.2:
            raise RuntimeError(f"Drone {drone_id} does not seem to be airborne; take off from Skybrush first.")
    for a in range(len(ids)):
        for b in range(a):
            if np.linalg.norm(starts[a] - starts[b]) < 1.5 * MIN_DISTANCE:
                raise RuntimeError(f"Drones {ids[a]} and {ids[b]} start too close to each other.")

    clients = [] if (args.sim and args.no_server) else open_sessions(args, ids)
    if clients:
        print("Sessions: " + ", ".join(f"[{c.drone_id}]" for c in clients))
    max_error = np.zeros(len(ids))
    ticks = 0
    reason = "mission finished"
    try:
        t0 = time.perf_counter()
        next_t = t0
        while (t := time.perf_counter() - t0) < MISSION_DURATION:
            states = [d.state() for d in drones]
            lost = [i for i, st in zip(ids, states) if st is None]
            if lost:
                reason = f"mocap of drone(s) {', '.join(lost)} lost"
                break
            frames, status = [], []
            for k, ((pos, vel, rot), start) in enumerate(zip(states, starts)):
                pos_ref = reference(t, start, args.step)
                error = float(np.linalg.norm(pos - pos_ref))
                max_error[k] = max(max_error[k], error)
                tilt = float(np.arccos(np.clip(rot[2, 2], -1.0, 1.0)))
                if error > MAX_POSITION_ERROR or tilt > MAX_TILT:
                    reason = (f"safety limit on drone {ids[k]} (position error {error:.2f} m, "
                              f"tilt {np.degrees(tilt):.0f} deg)")
                    break
                thrust, rates = controllers[k](pos, vel, rot, pos_ref, yaw_refs[k])
                roll, pitch, yaw = to_wire(rates)
                frames.append((thrust, roll, pitch, yaw))
                status.append(f"{ids[k]}: pos={np.round(pos, 3)} ref={np.round(pos_ref, 3)} T={thrust:.3f} N")
            else:
                distances = [np.linalg.norm(states[a][0] - states[b][0]) for a in range(len(ids)) for b in range(a)]
                if distances and min(distances) < MIN_DISTANCE:
                    reason = f"drones closer than {MIN_DISTANCE} m"
                    break
                packed = [MpcClient.frame(*f) for f in frames]
                if args.per_drone:
                    for client, frame in zip(clients, packed):
                        client.send_frames([frame])
                elif clients:
                    clients[0].send_frames(packed)
                ticks += 1
                if args.sim:
                    for drone, f in zip(drones, frames):
                        drone.apply(*f, dt)
                if int(t / dt) % int(args.rate) == 0:
                    print(f"t={t:5.2f} " + " | ".join(status))
                next_t += dt
                time.sleep(max(0.0, next_t - time.perf_counter()))
                continue
            break  # safety limit
    except KeyboardInterrupt:
        reason = "interrupted"
    except OSError as exc:  # e.g. the server ended the session and closed the connection
        reason = f"connection error: {exc!r}"
    finally:
        for client in clients:
            client.close()
    print(f"Session(s) ended: {reason}. Sent {ticks} ticks. Server policy: {args.policy}.")
    for drone_id, err in zip(ids, max_error):
        print(f"  drone {drone_id}: max position error {err:.3f} m")
    for client in clients:
        if client.log_records:
            print(f"  [{client.drone_id}]: {client.log_records} onboard log records")
    if mocap is not None and not mocap.close():
        # The reader thread is blocked in the C++ library, which would abort at interpreter exit. The session is
        # already closed, so skip the destructors.
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
