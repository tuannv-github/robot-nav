#!/usr/bin/env python3
"""Read Livox Mid-360 point packets and push them to the ZMQ server."""

from __future__ import annotations

import os
import socket
import struct
import time

import zmq


def main() -> None:
    host = os.environ.get("LIDAR_UDP_HOST", "192.168.123.164").strip()
    port = int(os.environ.get("LIDAR_UDP_PORT", "56301"))
    endpoint = os.environ.get("ZMQ_LIDAR_PUSH", "tcp://10.1.137.133:5601").strip()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind((host, port))
    udp.settimeout(1.0)

    ctx = zmq.Context()
    push = ctx.socket(zmq.PUSH)
    push.setsockopt(zmq.LINGER, 0)
    push.setsockopt(zmq.SNDHWM, 2000)
    push.connect(endpoint)

    print(f"lidar udp {host}:{port} -> {endpoint}", flush=True)
    packets = 0
    points = 0
    drops = 0
    window = time.monotonic()
    try:
        while True:
            try:
                data, _addr = udp.recvfrom(65535)
            except socket.timeout:
                data = b""
            if data:
                try:
                    push.send(data, flags=zmq.NOBLOCK)
                except zmq.Again:
                    drops += 1
                else:
                    packets += 1
                    if len(data) >= 7:
                        points += struct.unpack_from("<H", data, 5)[0]
            now = time.monotonic()
            if now - window >= 5:
                elapsed = now - window
                print(
                    "forward %d pkt/s  %d pt/s  drop %d"
                    % (round(packets / elapsed), round(points / elapsed), drops),
                    flush=True,
                )
                packets = points = drops = 0
                window = now
    finally:
        push.close(linger=0)
        ctx.term()
        udp.close()


if __name__ == "__main__":
    main()
