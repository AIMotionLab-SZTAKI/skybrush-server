import trio
from trio import sleep
from typing import Dict


def start() -> bytes:
    return f"CMDSTART_start_relative_EOF".encode()


def takeoff(h) -> bytes:
    return f"CMDSTART_takeoff_{h:.4f}_EOF".encode()


def land() -> bytes:
    return f"CMDSTART_land_EOF".encode()


def hover() -> bytes:
    return f"CMDSTART_hover_EOF".encode()



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
        raise NotImplementedError # immediately stop if we couldn't reach one of the drones

PORT = 6000
drones = ["09", "04", "07"]


async def takeoff_all(sockets: Dict[str, trio.SocketStream]):
    usr_input = input('type "takeoff"\n>')
    while usr_input != "takeoff":
        usr_input = input('type "takeoff"\n>')
    for drone_ID, socket in sockets.items():
        height = float(drone_ID)/10 + 0.3
        await socket.send_all(takeoff(height))
        ack = b""
        while ack != b"ACK":
            ack = await socket.receive_some()
            await sleep(0.01)


async def land_all(sockets: Dict[str, trio.SocketStream]):
    usr_input = input('type "land"\n>')
    while usr_input != "land":
        usr_input = input('type "land"\n>')
    for drone_ID, socket in sockets.items():
        await socket.send_all(land())
        ack = b""
        while ack != b"ACK":
            ack = await socket.receive_some()
            await sleep(0.01)


async def demo():
    print("Welcome to a test script!")
    await sleep(0.5)
    sockets: Dict[str, trio.SocketStream] = {}
    for drone_ID in drones:
        socket: trio.SocketStream = await establish_connection_with_handler(drone_ID)
        assert socket is not None
        sockets[drone_ID] = socket
    await takeoff_all(sockets)
    await land_all(sockets)

trio.run(demo)
