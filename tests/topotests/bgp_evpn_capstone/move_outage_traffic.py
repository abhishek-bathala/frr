#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
#
# move_outage_traffic.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
Traffic helper for test_evpn_capstone_move_outage.py.

Runs inside a topotest host namespace:

  echo    UDP echo responder (the robot).
  stream  Send a numbered UDP probe every --interval seconds and print one
          line per event (the controller):
              S <seq> <send_time>
              R <seq> <recv_time>
          Times are time.time() values, so they line up with the kernel
          monitor timestamps the test collects on the leaves.
  arp     Send ARP requests "who has --target, tell --src-ip" out --iface
          (the robot announcing itself after a roam).
  agent   Roam on command (the robot). Reads one line per move from stdin:
              roam <old_if> <new_if> <ip/len> <gw> <arp_count> <arp_interval>
          and prints "T <t_off> <t_on> <t_arp>": when the switch started, when
          the robot could send and receive on the new link, and when the
          first ARP went out. The process is already running, so a move costs one
          ip and one tc call instead of a shell pipeline.
"""

import argparse
import select
import socket
import struct
import subprocess
import sys
import threading
import time


def run_echo(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    while True:
        data, peer = sock.recvfrom(2048)
        sock.sendto(data, peer)


def run_stream(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    out = sys.stdout
    seq = 0
    start = time.time()
    next_send = start
    end = start + args.duration if args.duration > 0 else float("inf")

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

        timeout = max(0.0, next_send - time.time())
        ready, _, _ = select.select([sock], [], [], timeout)
        while ready:
            try:
                data = sock.recv(2048)
            except BlockingIOError:
                break
            if len(data) >= 4:
                out.write(
                    "R {} {:.6f}\n".format(struct.unpack("!I", data[:4])[0], time.time())
                )
        out.flush()


def _mac_bytes(mac):
    return bytes(int(part, 16) for part in mac.split(":"))


def _arp_socket(iface, src_ip, target):
    "Return (socket, frame) for an ARP request out iface."
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((iface, 0))
    src_mac = sock.getsockname()[4][:6]
    frame = (
        b"\xff" * 6
        + src_mac
        + struct.pack("!H", 0x0806)
        # Ethernet/IPv4, 6-byte MAC, 4-byte IP, ARP request.
        + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
        + src_mac
        + socket.inet_aton(src_ip)
        + b"\x00" * 6
        + socket.inet_aton(target)
    )
    return sock, frame


def run_arp(args):
    sock, frame = _arp_socket(args.iface, args.src_ip, args.target)
    for i in range(args.count):
        sock.send(frame)
        print("A {} {:.6f}".format(i, time.time()), flush=True)
        if i + 1 < args.count:
            time.sleep(args.interval)


def _batch(tool, commands):
    subprocess.run([tool, "-batch", "-"], input="\n".join(commands) + "\n",
                   text=True, check=True)


def run_agent(args):
    pref = args.radio_pref
    # Warm up ip and tc so the first move is not slowed by cold caches.
    _batch("ip", ["link show lo"])
    _batch("tc", ["qdisc show dev lo"])
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 7 or parts[0] != "roam":
            continue
        old_if, new_if, prefix, gw = parts[1:5]
        count, interval = int(parts[5]), float(parts[6])
        addr = prefix.split("/")[0]
        sock, frame = _arp_socket(new_if, addr, gw)

        # Radio off on the old link and on at the new one, then send replies
        # out the new link. The robot is reachable again once both are done.
        t_off = time.time()
        _batch("tc", [
            "filter add dev {} ingress pref {} matchall action drop".format(old_if, pref),
            "filter add dev {} egress pref {} matchall action drop".format(old_if, pref),
            "filter del dev {} ingress pref {}".format(new_if, pref),
            "filter del dev {} egress pref {}".format(new_if, pref),
        ])
        _batch("ip", ["addr replace {} dev {}".format(prefix, new_if),
                      "route replace default via {} dev {}".format(gw, new_if)])
        t_on = time.time()
        sock.send(frame)
        t_arp = time.time()
        print("T {:.6f} {:.6f} {:.6f}".format(t_off, t_on, t_arp), flush=True)
        _batch("ip", ["addr flush dev {}".format(old_if)])

        def rearp(sock=sock, frame=frame, count=count, interval=interval):
            for _ in range(count):
                time.sleep(interval)
                sock.send(frame)
            sock.close()

        threading.Thread(target=rearp, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("echo")
    p.add_argument("--port", type=int, required=True)

    p = sub.add_parser("stream")
    p.add_argument("--target", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--interval", type=float, default=0.01)
    p.add_argument("--duration", type=float, default=0)

    p = sub.add_parser("arp")
    p.add_argument("--iface", required=True)
    p.add_argument("--src-ip", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.1)

    p = sub.add_parser("agent")
    p.add_argument("--radio-pref", type=int, default=10)

    args = parser.parse_args()
    {"echo": run_echo, "stream": run_stream, "arp": run_arp,
     "agent": run_agent}[args.mode](args)


if __name__ == "__main__":
    main()
