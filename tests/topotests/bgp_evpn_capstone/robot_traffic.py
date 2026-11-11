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
  agent   Switch the robot's links on command (the robot). It is already
          running, so a switch costs one "ip -batch" call and the ARP is sent
          from this process. Reads one command per line from stdin:
              break <old_if> <new_if> <ip/len>   drop the old link
                  -> "B <t_break>"
              make <new_if> <ip/len> <gw>        bring up the new link, ARP
                  -> "M <t_make> <t_arp>"
              move <old_if> <new_if> <ip/len> <gw>   both, in one ip call
                  -> "V <t_break> <t_make> <t_arp>"
"""

import argparse
import select
import socket
import struct
import subprocess
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


def _send_arp(iface, prefix, gw):
    "Send one ARP request 'who has gw, tell <our IP>' out iface; return when."
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((iface, 0))
    mac = sock.getsockname()[4][:6]
    sock.send(
        b"\xff" * 6 + mac + struct.pack("!H", 0x0806)
        # Ethernet/IPv4, 6-byte MAC, 4-byte IP, ARP request.
        + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
        + mac + socket.inet_aton(prefix.split("/")[0])
        + b"\x00" * 6 + socket.inet_aton(gw)
    )
    sock.close()
    return time.time()


def _ip_batch(commands):
    subprocess.run(["ip", "-batch", "-"], input="\n".join(commands) + "\n",
                   text=True, check=True)


def _break_cmds(old_if, new_if, prefix):
    return ["link set {} down".format(old_if),
            "addr flush dev {}".format(old_if),
            "neigh flush dev {}".format(new_if),
            "addr replace {} dev {}".format(prefix, new_if)]


def _make_cmds(new_if, gw):
    return ["link set {} up".format(new_if),
            "route replace default via {} dev {}".format(gw, new_if)]


def run_agent(args):
    # Warm up ip so the first switch is not slowed by cold caches.
    _ip_batch(["link show lo"])
    for line in sys.stdin:
        parts = line.split()
        if parts[:1] == ["break"] and len(parts) == 4:
            t_break = time.time()
            _ip_batch(_break_cmds(*parts[1:4]))
            print("B {:.6f}".format(t_break), flush=True)
        elif parts[:1] == ["make"] and len(parts) == 4:
            new_if, prefix, gw = parts[1:4]
            _ip_batch(_make_cmds(new_if, gw))
            t_make = time.time()
            print("M {:.6f} {:.6f}".format(t_make, _send_arp(new_if, prefix, gw)), flush=True)
        elif parts[:1] == ["move"] and len(parts) == 5:
            old_if, new_if, prefix, gw = parts[1:5]
            t_break = time.time()
            _ip_batch(_break_cmds(old_if, new_if, prefix) + _make_cmds(new_if, gw))
            t_make = time.time()
            print("V {:.6f} {:.6f} {:.6f}".format(
                t_break, t_make, _send_arp(new_if, prefix, gw)), flush=True)


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

    sub.add_parser("agent")

    args = parser.parse_args()
    {"echo": run_echo, "stream": run_stream, "agent": run_agent}[args.mode](args)


if __name__ == "__main__":
    main()
