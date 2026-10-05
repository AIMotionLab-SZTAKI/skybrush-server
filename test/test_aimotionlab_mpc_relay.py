import logging
import struct

import trio
from pytest import raises
from trio.testing import memory_stream_pair

from flockwave.server.ext.aimotionlab.mpc_relay import (
    MPC_FRAME_SIZE,
    MpcSession,
    parse_mpc_request,
    resolve_uav_id,
)

log = logging.getLogger("test")


def frame(seq: int) -> bytes:
    return struct.pack("<BBffffff", 20, 0, float(seq), 1.0, 2.0, 3.0, 0.0, 0.0)


class FakeHighLevelCommander:
    def __init__(self, calls):
        self.calls = calls

    async def go_to(self, x, y, z, *, duration, relative):
        self.calls.append(("go_to", (x, y, z), relative))


class FakeCrazyflie:
    def __init__(self, send_delay: float = 0.0):
        self.packets = []
        self.calls = []
        self.send_delay = send_delay
        self.high_level_commander = FakeHighLevelCommander(self.calls)

    async def send_packet(self, port, channel, data):
        await trio.sleep(self.send_delay)
        self.packets.append((port, channel, bytes(data)))


class FakeUAV:
    id = "07"

    def __init__(self, cf):
        self.cf = cf

    def _get_crazyflie(self):
        return self.cf

    async def land(self, velocity=0.5):
        self.cf.calls.append(("land", velocity))

    async def get_parameter(self, name, fetch=False):
        assert name == "bodyrate.mpc_rx"
        # Pretend the firmware consumed every packet sent on the setpoint channel
        return len([p for p in self.cf.packets if p[:2] == (7, 0)])


def test_parse_mpc_request():
    assert parse_mpc_request("REQ_07") == ("07", "land", 0.5)
    assert parse_mpc_request("REQ_07_land_0.3") == ("07", "land", 0.3)
    assert parse_mpc_request("REQ_07_hover_2") == ("07", "hover", 2.0)
    assert parse_mpc_request("REQ_07_none") == ("07", "none", None)
    for bad in ("", "REQ_", "07", "REQ_07_fly"):
        with raises(ValueError):
            parse_mpc_request(bad)


def test_resolve_uav_id():
    ids = ["04", "08", "10", "bb1"]
    assert resolve_uav_id("08", ids) == "08"
    assert resolve_uav_id("8", ids) == "08"
    assert resolve_uav_id("008", ids) == "08"
    assert resolve_uav_id("bb1", ids) == "bb1"
    assert resolve_uav_id("9", ids) is None
    assert resolve_uav_id("xyz", ids) is None


async def test_frames_split_across_reads_are_reassembled_and_session_lands():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "land", 0.3)
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        data = frame(1) + frame(2)
        # Split the frames at awkward places, like TCP may do
        await client.send_all(data[:10])
        await trio.sleep(0.01)
        await client.send_all(data[10:30])
        await trio.sleep(0.01)
        await client.send_all(data[30:])
        await trio.sleep(0.01)
        await client.send_all(b"\x00")

    setpoints = [p for p in cf.packets if p[:2] == (7, 0)]
    assert [p[2] for p in setpoints] == [frame(1), frame(2)]
    assert all(len(p[2]) == MPC_FRAME_SIZE for p in setpoints)
    # notify setpoints stop, then land
    assert cf.packets[-1] == (7, 1, struct.pack("<BI", 0, 0))
    assert cf.calls == [("land", 0.3)]


async def test_busy_radio_sends_latest_frame_only():
    cf = FakeCrazyflie(send_delay=0.05)
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "none", None)
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        await client.send_all(frame(1))
        await trio.sleep(0.01)  # frame 1 is now being sent
        await client.send_all(frame(2) + frame(3) + frame(4))
        await trio.sleep(0.2)
        await client.send_all(b"\x00")

    assert [p[2] for p in cf.packets] == [frame(1), frame(4)]
    assert session.frames_received == 4
    assert cf.calls == []  # policy "none": no handover


async def test_stream_timeout_ends_session_with_hover():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "hover", 0.01)
    with trio.fail_after(2):
        async with trio.open_nursery() as nursery:
            nursery.start_soon(session.run)
            await client.send_all(frame(1))
            # ...and then the client hangs without closing the connection

    assert cf.calls == [("go_to", (0.0, 0.0, 0.0), True), ("land", 0.5)]


async def test_disconnect_before_first_frame_does_nothing():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "land", 0.5)
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        await client.aclose()

    assert cf.packets == [] and cf.calls == []
