#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
#
# robot_traffic.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
Traffic helper for test_evpn_capstone_break_before_make.py.

Runs inside a topotest host namespace:

  echo    UDP echo responder (the robot).
  stream  Send a numbered UDP probe every --interval seconds (the controller)
          and print one line per event:
              S <seq> <send_time>
              R <seq> <recv_time>
          Times are time.time() values, so they line up with the kernel
          monitor timestamps the test collects on the leaves.
"""

import argparse
import select
import socket
import struct
import sys
import time


def run_echo(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    while True:
        data, peer = sock.recvfrom(2048)
        try:
            sock.sendto(data, peer)
        except OSError:
            # No route while the robot is between leaves: drop the reply.
            pass


def run_stream(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    out = sys.stdout
    seq = 0
    next_send = time.time()
    end = next_send + args.duration

    while True:
        now = time.time()
        if now >= next_send:
            if now >= end:
                break
            sock.sendto(struct.pack("!I", seq), (args.target, args.port))
            out.write("S {} {:.6f}\n".format(seq, now))
            seq += 1
            next_send += args.interval
            # Never try to catch up after a scheduling hiccup.
            if next_send < now:
                next_send = now + args.interval

        ready, _, _ = select.select([sock], [], [], max(0.0, next_send - time.time()))
        while ready:
            try:
                data = sock.recv(2048)
            except BlockingIOError:
                break
            if len(data) >= 4:
                out.write("R {} {:.6f}\n".format(struct.unpack("!I", data[:4])[0], time.time()))
        out.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("echo")
    p.add_argument("--port", type=int, required=True)

    p = sub.add_parser("stream")
    p.add_argument("--target", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--interval", type=float, default=0.01)
    p.add_argument("--duration", type=float, required=True)

    args = parser.parse_args()
    {"echo": run_echo, "stream": run_stream}[args.mode](args)


if __name__ == "__main__":
    main()
