# echo-client.py

import sys
import trio
from trio import sleep


def cw() -> bytes:
    with open(f"cw_traj.json", 'r') as f:
        traj = f.read()
    return f"CMDSTART_upload_{traj}_EOF".encode()


def ccw() -> bytes:
    with open(f"ccw_traj.json", 'r') as f:
        traj = f.read()
    return f"CMDSTART_upload_{traj}_EOF".encode()


def start() -> bytes:
    return f"CMDSTART_start_relative_EOF".encode()


def takeoff(h) -> bytes:
    return f"CMDSTART_takeoff_{h:.4f}_EOF".encode()


def land() -> bytes:
    return f"CMDSTART_land_EOF".encode()


def hover() -> bytes:
    return f"CMDSTART_hover_EOF".encode()


# list of commands to be dispatched, first a delay to be waited before the command, then the command to be sent
demo_maneuvers = [(1, takeoff(0.3)),
                  (3, cw()),
                  (1, start()),
                  (3, hover()),
                  (2, cw()),
                  (1, start()),
                  (1, ccw()),
                  (5, start()),
                  (1, ccw()),
                  (3, start()),
                  (5, land())]



async def establish_connection_with_handler(drone_id: str):
    drone_stream: trio.SocketStream = await trio.open_tcp_stream("127.0.0.1", PORT)
    await sleep(0.01)
    request = f"REQ_{drone_id}"
    print(f"Requesting handler for drone {drone_id}")
    await drone_stream.send_all(request.encode('utf-8'))
    acknowledgement: bytes = await drone_stream.receive_some()
    if acknowledgement.decode('utf-8') == f"ACK_{drone_id}":
        print(f"successfully created server-side handler for drone {drone_id}")
        return drone_stream
    else:
        return None

PORT = 6000
drone = "09"

async def demo():
    print("Welcome to a test demo!")
    await sleep(0.5)
    socket: trio.SocketStream = await establish_connection_with_handler(drone)
    assert socket is not None
    for delay, command in demo_maneuvers:
        await sleep(delay)
        print(command)
        await socket.send_all(command)
        ack = b""
        while ack != b"ACK":
            ack = await socket.receive_some()
            await sleep(0.01)
        print(ack)

trio.run(demo)
