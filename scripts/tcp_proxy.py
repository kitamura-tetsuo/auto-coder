#!/usr/bin/env python3
"""TCP Proxy to expose localhost ports on 0.0.0.0"""

import socket
import sys
import threading


def forward(source, destination):
    """Forward data from source to destination"""
    try:
        while True:
            data = source.recv(4096)
            if not data:
                break
            destination.sendall(data)
    except:
        pass
    finally:
        source.close()
        destination.close()


def handle_client(client_socket, target_host, target_port):
    """Handle a client connection by proxying to target"""
    try:
        # Connect to the target server
        target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_socket.connect((target_host, target_port))

        # Start bidirectional forwarding
        client_to_target = threading.Thread(target=forward, args=(client_socket, target_socket))
        target_to_client = threading.Thread(target=forward, args=(target_socket, client_socket))

        client_to_target.start()
        target_to_client.start()

        client_to_target.join()
        target_to_client.join()
    except Exception as e:
        print(f"Error handling client: {e}", file=sys.stderr)
        client_socket.close()


def start_proxy(listen_port, target_host, target_port):
    """Start a TCP proxy server"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(5)

    print(f"Proxying 0.0.0.0:{listen_port} -> {target_host}:{target_port}")

    while True:
        client_socket, addr = server.accept()
        client_handler = threading.Thread(target=handle_client, args=(client_socket, target_host, target_port))
        client_handler.daemon = True
        client_handler.start()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: tcp_proxy.py <listen_port> <target_host> <target_port>")
        sys.exit(1)

    listen_port = int(sys.argv[1])
    target_host = sys.argv[2]
    target_port = int(sys.argv[3])

    start_proxy(listen_port, target_host, target_port)
