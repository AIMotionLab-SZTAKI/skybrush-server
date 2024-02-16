import pickle
import socket
import time
import pickle
import numpy
#
# def send(socket, msg):


def wait_start(stream: socket.socket):
    msg = b""
    while msg != b"START":
        msg = stream.recv(1024)
    print(f"got {msg} from server")


def wait_until(t):
    sleep_time = t - time.time()
    if sleep_time > 0:
        time.sleep(sleep_time)


def main():
    ip = "127.0.0.1"
    port = 6003
    cf_id = "03"
    with open("lqr_params_bb_load.pickle", "rb") as file:
        data = pickle.load(file)
    try:
        client_socket = socket.socket()
        client_socket.connect((ip, port))
    except Exception as e:
        print(f"Error: {e.__repr__()}")
        return
    client_socket.sendall(cf_id.encode())
    print(f"Connected to skybrush at port {port}")

    time.sleep(0.5)
    wait_start(client_socket)

    start_time = time.time()
    try:
        for timestamp, K in data:
            while len(K) % 6:
                K += [0.0]
            wait_until(start_time + timestamp)
            print(f"{time.time() - start_time}: sending params for timestamp {timestamp}")
            num_lqr_packets = int(len(K) / 6) + 1 if len(K) % 6 != 0 else int(len(K) / 6)
            for idx in range(num_lqr_packets):
                params = K[idx*6: (idx+1)*6]
                channel = 2
                port = 1
                data = [idx, int(timestamp*1000)] + params
                msg = pickle.dumps((port, channel, data))
                client_socket.sendall(msg)
                time.sleep(2 / 1000)
    finally:
        client_socket.close()


if __name__ == '__main__':
    main()
