#!/usr/bin/env python3
# SPDX-License-Identifier: ISC

#
# warehouse_traffic.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
warehouse_traffic.py: Controller <-> robot instruction stream for topotests.

Run inside topotest host namespaces by test_evpn_capstone_warehouse.py:

  echo:  on the robot. Sends every UDP datagram straight back to its sender.

  send:  on the controller. Sends one numbered datagram every --interval
         seconds for --duration seconds, collects the echoes, then prints one
         JSON object to stdout:

           {"interval": 0.01,
            "sent": [t_send, ...],          # index = sequence number
            "recv": [[seq, t_recv], ...]}   # first echo of each sequence

All times are time.time() (CLOCK_REALTIME), which is shared by every network
and time namespace, so the test can line them up with its own move events.

Both modes exit on their own after --duration, so the test never has to signal
a process inside a namespace.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

# Sequence number, send time.
PAYLOAD = struct.Struct("!Id")


def run_echo(port, duration):
    "Echo datagrams on port until duration seconds have passed."
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    end = time.time() + duration
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        try:
            sock.sendto(data, addr)
        except OSError:
            # No route while the robot is between access points.
            pass


def run_send(target, port, interval, duration, linger):
    "Send numbered datagrams to target and print what came back as JSON."
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sent = []
    recv = {}
    stop = threading.Event()

    def receiver():
        while not stop.is_set():
            try:
                data = sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                continue
            t_recv = time.time()
            if len(data) < PAYLOAD.size:
                continue
            seq, _ = PAYLOAD.unpack_from(data)
            recv.setdefault(seq, t_recv)

    thread = threading.Thread(target=receiver, daemon=True)
    thread.start()

    # Schedule against the start time so sleep jitter does not add up.
    start = time.time()
    count = int(duration / interval)
    for seq in range(count):
        delay = start + seq * interval - time.time()
        if delay > 0:
            time.sleep(delay)
        t_send = time.time()
        sent.append(t_send)
        try:
            sock.sendto(PAYLOAD.pack(seq, t_send), (target, port))
        except OSError:
            # Counted as lost: the send time is recorded, no echo will arrive.
            pass

    time.sleep(linger)
    stop.set()
    thread.join(1)

    json.dump(
        {
            "interval": interval,
            "sent": sent,
            "recv": sorted(recv.items()),
        },
        sys.stdout,
    )
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="mode", required=True)

    echo = sub.add_parser("echo", help="echo datagrams back (robot side)")
    echo.add_argument("--port", type=int, default=5005)
    echo.add_argument("--duration", type=float, required=True)

    send = sub.add_parser("send", help="send the stream (controller side)")
    send.add_argument("--target", required=True)
    send.add_argument("--port", type=int, default=5005)
    send.add_argument("--interval", type=float, default=0.01)
    send.add_argument("--duration", type=float, required=True)
    send.add_argument("--linger", type=float, default=1.0,
                      help="seconds to keep listening after the last send")

    args = parser.parse_args()
    if args.mode == "echo":
        run_echo(args.port, args.duration)
    else:
        run_send(args.target, args.port, args.interval, args.duration,
                 args.linger)


if __name__ == "__main__":
    main()
