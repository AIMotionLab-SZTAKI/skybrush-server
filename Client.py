# echo-client.py

import sys
import trio
from trio import sleep
PORT = 6000

commands = {
    b"takeoff": ((True, float), False),
    b"traj": ((True, str), True),
    b"land": ((False, None), False),
    b"hover": ((False, None), False),
}


async def build_command() -> bytes:
    # Prompt user for ID (integer from 0 to 9)
    while True:
        try:
            id_ = int(input("Enter ID (0-9): "))
            await sleep(0.1)
            if 0 <= id_ <= 9:
                break
        except ValueError:
            pass
        print("Invalid ID. Please enter an integer from 0 to 9.")

    # Prompt user for command (must be in dictionary)
    while True:
        command = input("Enter command: ").encode()
        await sleep(0.1)
        if command in commands:
            break
        print("Invalid command. Valid commands are: ", list(commands.keys()))

    # Build command string
    arg_argtype, payload = commands[command]
    if arg_argtype[0]:
        while True:
            try:
                arg_str = arg_argtype[1](input("Enter argument: "))
                await sleep(0.1)
                break
            except ValueError:
                pass
            print(f"Invalid argument. Please enter a {arg_argtype[1].__name__}.")
        arg = str(arg_str).encode() + b'_'
    else:
        arg = b""
    if payload:
        payload = input("Enter payload name: ")
        await sleep(0.1)
        with open(f"{payload}_traj.json", 'rb') as f:
            payload = f.read()
        payload = payload + b'_'
    else:
        payload = b""
    return b"CMDSTART_" + str(id_).encode() + b"_" + command + b"_" + arg + payload + b"EOF"


async def sender(client_stream: trio.SocketStream):
    print("sender: started!")
    while True:
        data = await build_command()
        await client_stream.send_all(data)
        print(f"sender: sending {data}")
        await trio.sleep(0.5)


def cw(ID):
    with open (f"cw_traj.json", 'rb') as f:
        payload = f.read()
    return b'CMDSTART_'+ ID.encode('utf-8') + b'_traj_relative_' + payload + b'_EOF'


def ccw(ID):
    with open (f"ccw_traj.json", 'rb') as f:
        payload = f.read()
    return b'CMDSTART_'+ ID.encode('utf-8') + b'_traj_relative_' + payload + b'_EOF'


def traj(ID):
    with open (f"trajectory.json", 'rb') as f:
        payload = f.read()
    return b'CMDSTART_'+ ID.encode('utf-8') + b'_traj_relative_' + payload + b'_EOF'


shortcut_dict = {
    "takeoff": b'CMDSTART_0_takeoff_0.6_EOF',
    "takeoff6": b'CMDSTART_6_takeoff_0.6_EOF',
    "takeoff8": b'CMDSTART_8_takeoff_0.4_EOF',
    "land": b'CMDSTART_0_land_EOF',
    "land6": b'CMDSTART_6_land_EOF',
    "land8": b'CMDSTART_8_land_EOF',
    "hover": b'CMDSTART_0_hover_EOF',
    "hover6": b'CMDSTART_6_hover_EOF',
    "hover8": b'CMDSTART_8_hover_EOF',
    "cw": cw(str(0)),
    "cw6": cw(str(6)),
    "cw8": cw(str(8)),
    "ccw": ccw(str(0)),
    "ccw6": ccw(str(6)),
    "ccw8": ccw(str(8)),
    "traj": traj(str(0))
}


async def shortcut_sender(client_stream: trio.SocketStream):
    print("sender: Started!")
    while True:
        data = input("Type command: ")
        if data in shortcut_dict:
            await client_stream.send_all(shortcut_dict[data])
            print(f"sender: sending {data}")
            print(shortcut_dict[data])
        else:
            print("No such command shortcut.")
        await trio.sleep(0.5)


async def receiver(client_stream):
    print("receiver: started!")
    async for data in client_stream:
        if type(data) == bytes:
            data = data.decode("utf-8")
        print(f"receiver: got the following data: {data}")
    print("receiver: connection closed")
    sys.exit()


async def parent():
    # client_stream = await trio.open_tcp_stream("192.168.1.78", PORT)
    client_stream = await trio.open_tcp_stream("127.0.0.1", PORT)
    Mode = 'S'
    # Mode = None
    while Mode != 'S' and Mode != 's' and Mode != 'P' and Mode != 'p':
        Mode = input("Do you want to type commands with Shortcuts (S) or with Prompts (P)?:")

    async with client_stream:
        async with trio.open_nursery() as nursery:
            print("parent: spawning sender...")
            if Mode == 'p' or Mode == 'P':
                nursery.start_soon(sender, client_stream)
            elif Mode == 's' or Mode == 'S':
                nursery.start_soon(shortcut_sender, client_stream)
            print("parent: spawning receiver...")
            nursery.start_soon(receiver, client_stream)
trio.run(parent)
