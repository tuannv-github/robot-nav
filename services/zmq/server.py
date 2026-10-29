#!/usr/bin/env python3
"""ZMQ broker for G1 lidar and control.

Lidar
  PULL tcp://0.0.0.0:5601   robot pushes raw Livox Ethernet packets,
                            or multipart [b"lidar", packet] / [b"odom", json]
  PUB  tcp://0.0.0.0:5600   subscribers receive [b"lidar", packet] or [b"odom", json]

Control
  SUB  tcp://0.0.0.0:5602   publishers send a JSON object
                            {type: cmd_vel|stick|stop, ...}
  PUSH tcp://<robot>:8082   forwarded to the G1 controller PULL socket

A Livox packet is the Mid-360 UDP payload (data_type 1: Cartesian, mm).
"""

from __future__ import annotations

import json
import os
import signal
import struct
import time

import zmq

stop = False


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value or default


def _json_object(raw: bytes) -> bool:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(body, dict)


def _points(raw: bytes) -> int:
    if len(raw) < 7:
        return 0
    return struct.unpack_from("<H", raw, 5)[0]


def _request_stop(_signum, _frame) -> None:
    global stop
    stop = True


def main() -> None:
    lidar_pub_ep = _env("ZMQ_LIDAR_PUB", "tcp://0.0.0.0:5600")
    lidar_pull_ep = _env("ZMQ_LIDAR_PULL", "tcp://0.0.0.0:5601")
    control_sub_ep = _env("ZMQ_CONTROL_SUB", "tcp://0.0.0.0:5602")
    control_push_ep = _env("ZMQ_CONTROL_PUSH", "tcp://10.1.100.211:8082")
    stats_sec = float(os.environ.get("ZMQ_STATS_SEC", "5"))
    health = os.environ.get("ZMQ_HEALTH_FILE", "/tmp/zmq-healthy")

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pull = ctx.socket(zmq.PULL)
    sub = ctx.socket(zmq.SUB)
    push = ctx.socket(zmq.PUSH)
    for sock, hwm in ((pub, 2000), (pull, 2000), (sub, 200), (push, 200)):
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SNDHWM, hwm)
        sock.setsockopt(zmq.RCVHWM, hwm)
    push.setsockopt(zmq.IMMEDIATE, 1)
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    pub.bind(lidar_pub_ep)
    pull.bind(lidar_pull_ep)
    sub.bind(control_sub_ep)
    if control_push_ep.lower() not in ("", "off", "none"):
        push.connect(control_push_ep)
    else:
        control_push_ep = ""

    with open(health, "w", encoding="utf-8") as handle:
        handle.write("ok\n")

    print(f"lidar pub  {lidar_pub_ep}", flush=True)
    print(f"lidar pull {lidar_pull_ep}", flush=True)
    print(f"control sub  {control_sub_ep}", flush=True)
    print(f"control push {control_push_ep or 'off'}", flush=True)

    poller = zmq.Poller()
    poller.register(pull, zmq.POLLIN)
    poller.register(sub, zmq.POLLIN)

    lidar_pkts = 0
    lidar_pts = 0
    lidar_drop = 0
    odom_msgs = 0
    odom_drop = 0
    control_n = 0
    control_drop = 0
    control_bad = 0
    window = time.monotonic()

    while not stop:
        try:
            ready = dict(poller.poll(1000))
        except zmq.ZMQError:
            break

        if pull in ready:
            while not stop:
                try:
                    parts = pull.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    break
                raw = parts[-1]
                topic = parts[0] if len(parts) >= 2 and parts[0] in (b"lidar", b"odom") else b"lidar"
                try:
                    pub.send_multipart([topic, raw], flags=zmq.NOBLOCK)
                except zmq.Again:
                    if topic == b"odom":
                        odom_drop += 1
                    else:
                        lidar_drop += 1
                    continue
                if topic == b"odom":
                    odom_msgs += 1
                else:
                    lidar_pkts += 1
                    lidar_pts += _points(raw)

        if sub in ready:
            while not stop:
                try:
                    parts = sub.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    break
                body = parts[-1]
                if not _json_object(body):
                    control_bad += 1
                    continue
                if not control_push_ep:
                    control_n += 1
                    continue
                try:
                    push.send(body, flags=zmq.NOBLOCK)
                except zmq.Again:
                    control_drop += 1
                    continue
                control_n += 1

        now = time.monotonic()
        if now - window >= stats_sec:
            elapsed = now - window
            print(
                "lidar %d pkt/s  %d pt/s  drop %d | odom %d msg/s  drop %d | control %d  drop %d  bad %d"
                % (
                    round(lidar_pkts / elapsed),
                    round(lidar_pts / elapsed),
                    lidar_drop,
                    round(odom_msgs / elapsed),
                    odom_drop,
                    control_n,
                    control_drop,
                    control_bad,
                ),
                flush=True,
            )
            lidar_pkts = lidar_pts = lidar_drop = 0
            odom_msgs = odom_drop = 0
            control_n = control_drop = control_bad = 0
            window = now

    for sock in (pub, pull, sub, push):
        sock.close(linger=0)
    ctx.term()
    try:
        os.remove(health)
    except OSError:
        pass


if __name__ == "__main__":
    main()
