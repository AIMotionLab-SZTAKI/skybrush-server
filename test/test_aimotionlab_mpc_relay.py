import logging
import struct

import trio
from pytest import raises
from trio.testing import memory_stream_pair

import json

from flockwave.server.ext.aimotionlab.mpc_relay import (
    MPC_FRAME_SIZE,
    MPC_FRAME_SIZES,
    MPC_LOG_BLOCKS,
    MPC_LOG_PROFILES,
    split_mpc_options,
    MpcSession,
    parse_mpc_request,
    resolve_uav_id,
)

log = logging.getLogger("test")


def frame(seq: int) -> bytes:
    return struct.pack("<BBffffff", 20, 0, float(seq), 1.0, 2.0, 3.0, 0.0, 0.0)


def indi_frame(seq: int) -> bytes:
    return struct.pack("<BBfffffff", 21, 0, float(seq), 0.1, 0.2, 0.3, 4.0, 5.0, 6.0)


class FakeHighLevelCommander:
    def __init__(self, calls):
        self.calls = calls

    async def go_to(self, x, y, z, *, duration, relative):
        self.calls.append(("go_to", (x, y, z), relative))


class FakeLogMessage:
    def __init__(self, block, timestamp):
        self.block, self.timestamp = block, timestamp

    def to_dict(self):
        return {name: float(self.timestamp) for name in self.block.variables}


class FakeLogBlock:
    def __init__(self, variables):
        self.variables = variables


class FakeLogSession:
    def __init__(self, calls, fail):
        self.calls, self.fail, self.blocks = calls, fail, []

    def configure(self, **kwds):
        pass

    def create_block(self, *variables, frequency=None):
        if self.fail:
            raise KeyError("no such variable in log TOC: 'ctrlMpc.aff_x'")
        block = FakeLogBlock(variables)
        self.blocks.append(block)
        return block

    async def __aenter__(self):
        self.calls.append("log_start")

    async def __aexit__(self, *exc):
        self.calls.append("log_stop")

    async def messages(self):
        for k in range(3):
            for block in self.blocks:
                yield FakeLogMessage(block, 10 * k)
        await trio.sleep_forever()


class FakeLog:
    def __init__(self, calls, fail=False):
        self.calls, self.fail = calls, fail

    async def validate(self):
        pass

    def create_session(self):
        return FakeLogSession(self.calls, self.fail)


class FakeCrazyflie:
    def __init__(self, send_delay: float = 0.0, log_fails: bool = False):
        self.packets = []
        self.calls = []
        self.send_delay = send_delay
        self.high_level_commander = FakeHighLevelCommander(self.calls)
        self.log = FakeLog(self.calls, log_fails)

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
        if name != "mpcin.rx":
            raise KeyError(name)  # older firmware names are tried only if this one is missing
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


def test_frame_sizes_fit_crtp():
    assert MPC_FRAME_SIZES == {20: 26, 21: 30}  # CRTP payload is at most 30 bytes


async def test_type_21_frames_are_relayed_whole_and_types_can_mix():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "none", None)
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        data = indi_frame(1) + frame(2) + indi_frame(3)
        # Each read completes one frame (the relay is latest-wins), and splits fall inside both frame types
        for chunk in (data[:7], data[7:40], data[40:70], data[70:]):
            await client.send_all(chunk)
            await trio.sleep(0.01)
        await client.send_all(b"\x00")

    assert [p[2] for p in cf.packets if p[:2] == (7, 0)] == [indi_frame(1), frame(2), indi_frame(3)]


async def test_unknown_frame_type_ends_session():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "none", None)
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        await client.send_all(frame(1))
        await trio.sleep(0.01)
        await client.send_all(bytes([22]) + bytes(29))

    assert [p[2] for p in cf.packets if p[:2] == (7, 0)] == [frame(1)]


def test_split_mpc_options():
    assert split_mpc_options("REQ_07_land_0.3") == ("REQ_07_land_0.3", None, None)
    assert split_mpc_options("REQ_07_land_0.3_log") == ("REQ_07_land_0.3", 50.0, "mpc")
    assert split_mpc_options("REQ_07_log100") == ("REQ_07", 100.0, "mpc")
    assert split_mpc_options("REQ_07_land_0.3_log50:indi") == ("REQ_07_land_0.3", 50.0, "indi")
    assert split_mpc_options("REQ_07_log:indi") == ("REQ_07", 50.0, "indi")
    for bad in ("REQ_07_logx", "REQ_07_log0", "REQ_07_log500", "REQ_07_log50:nope"):
        with raises(ValueError):
            split_mpc_options(bad)


def test_log_blocks_fit_log_packets():
    sizes = {"ctrlMpc.type": 1, "ctrlMpc.state": 1, "indi.mode": 1}  # uint8; others float / uint32 (4 bytes)
    names = [name for blocks in MPC_LOG_PROFILES.values() for name in blocks]
    assert len(names) == len(set(names))
    for blocks in MPC_LOG_PROFILES.values():
        for variables in blocks.values():
            assert sum(sizes.get(v, 4) for v in variables) <= 26  # firmware LOG_MAX_LEN


async def read_lines(stream, into):
    buf = b""
    while True:
        try:
            data = await stream.receive_some()
        except trio.ClosedResourceError:
            return
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            into.append(json.loads(line))


async def test_onboard_log_is_streamed_and_removed_after_the_handover():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "land", 0.3, log_hz=50.0)
    records = []
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        nursery.start_soon(read_lines, client, records)
        await client.send_all(frame(1))
        await trio.sleep(0.05)
        await client.send_all(b"\x00")
        await trio.sleep(0.05)
        await client.aclose()

    assert len(records) == 3 * len(MPC_LOG_BLOCKS)
    assert {r["block"] for r in records} == set(MPC_LOG_BLOCKS)
    assert set(MPC_LOG_BLOCKS["a"]) <= set(records[0]) and {"t_fw", "t_srv"} <= set(records[0])
    # the log blocks are removed only after the drone was handed over (landing)
    assert cf.calls == ["log_start", ("land", 0.3), "log_stop"]
    assert [p[2] for p in cf.packets if p[:2] == (7, 0)] == [frame(1)]


async def test_log_failure_does_not_end_the_session():
    cf = FakeCrazyflie(log_fails=True)
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "none", None, log_hz=50.0)
    records = []
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        nursery.start_soon(read_lines, client, records)
        await trio.sleep(0.02)
        await client.send_all(frame(1))
        await trio.sleep(0.02)
        await client.send_all(frame(2))
        await trio.sleep(0.02)
        await client.send_all(b"\x00")
        await trio.sleep(0.05)
        await client.aclose()

    assert len(records) == 1 and "error" in records[0]
    assert [p[2] for p in cf.packets if p[:2] == (7, 0)] == [frame(1), frame(2)]


async def test_indi_log_profile_streams_its_blocks():
    cf = FakeCrazyflie()
    client, server = memory_stream_pair()
    session = MpcSession(FakeUAV(cf), server, log, "none", None, log_hz=50.0, log_profile="indi")
    records = []
    async with trio.open_nursery() as nursery:
        nursery.start_soon(session.run)
        nursery.start_soon(read_lines, client, records)
        await client.send_all(frame(1))
        await trio.sleep(0.05)
        await client.send_all(b"\x00")
        await trio.sleep(0.05)
        await client.aclose()

    assert {r["block"] for r in records} == set(MPC_LOG_PROFILES["indi"])
    assert "indi.u1" in next(r for r in records if r["block"] == "i1")
