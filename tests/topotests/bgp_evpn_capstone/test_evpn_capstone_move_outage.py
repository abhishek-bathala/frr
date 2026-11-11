#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_move_outage.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
test_evpn_capstone_move_outage.py: Show that every robot move costs an outage,
and that the outage lasts until the EVPN control plane catches up.

The fabric is the Capstone topology from test_evpn_capstone_mobility.py. The
robot is cabled to leaf1 and leaf2, and both access ports stay up and bridged
for the whole run, the way Wi-Fi access points stay up when a robot drives
away. A move ("roam") turns the robot's radio off on one link and on at the
other. The radio is a tc drop filter on the robot's side, so the old leaf
never sees its port go down: it keeps sending the robot's traffic to the old
port until EVPN tells it the robot has moved (RFC 7432 section 15). A roam
agent already running on the robot does the switch, so the robot itself adds
only a few milliseconds; the outage after its radio is back on is the
fabric's.

By default the leaves' bridges do not learn MACs from frames that arrive over
VXLAN, so only EVPN can move the robot, as in a typical EVPN deployment. The
Capstone fabric as configured does learn them: the old leaf then picks the
robot up from its flooded ARP and floods its traffic to it within tens of
milliseconds, before EVPN catches up. MOVE_OUTAGE_VXLAN_LEARNING=on shows that.

What the test records:

  * Instructions: the controller sends a numbered UDP probe to the robot every
    10 ms and the robot echoes it back. Every 10th probe is an instruction
    (one per 100 ms). An instruction is missed if its round trip is lost or
    takes longer than the budget (100 ms by default).

  * The update chain: "ip -ts monitor" and "bridge -timestamp monitor" run on
    every leaf for the whole test, so each leaf's change of mind about the
    robot (MAC, IP, /32 route) is timestamped by the kernel to the
    microsecond. No vtysh polling runs during a move, so the measurement does
    not slow the fabric down.

  * Evidence per move, written to <logdir>/move_outage/:
      move<N>_timeline.txt   the update chain, every instruction in the move
                             window, the raw kernel events, and EVPN state
      <leaf>_ip.log          raw ip monitor output for the whole run
      <leaf>_fdb.log         raw bridge monitor output for the whole run
      results.json           all measurements

The tests pass when the problem is reproduced: no instructions are lost while
the robot stands still, every move loses instructions, the outage does not end
until the control plane has moved the robot's path, and the worst fabric
outage is over the budget. A move is not counted if leaf5 had already lost the
robot's /32 when it started (the separate IP-relearn problem), because its
traffic then never depended on EVPN.

With the visualizer running (python3 visualizer_server.py), each leaf shows
where it thinks the robot is (orange = stale, pointing at the old leaf),
green and red dots show delivered and missed instructions, and the log panel
prints each move's timeline. Two seconds after each move, the visualizer
replays it 10 times slower from the recorded events, with yellow dots for the
EVPN update travelling through the spines.

Environment variables:
  MOVE_OUTAGE_MOVES          number of moves (default 4, below the DAD limit
                             of 5 moves in 180 s)
  MOVE_OUTAGE_GAP            seconds between moves (default 15)
  MOVE_OUTAGE_BUDGET_MS      instruction deadline in ms (default 100)
  MOVE_OUTAGE_BASELINE       seconds of instructions with no moves (default 10)
  MOVE_OUTAGE_REARP_COUNT    ARPs the robot sends after its first one, so the
                             new leaf relearns its IP and the IP-relearn
                             problem does not spill into the next move
                             (default 30)
  MOVE_OUTAGE_REARP_INTERVAL seconds between those ARPs (default 0.1)
  MOVE_OUTAGE_VXLAN_LEARNING "off" (default): EVPN only. "on": the Capstone
                             fabric as configured, with data-plane learning
                             on the VXLAN ports.
  MOVE_OUTAGE_REPLAY_SLOWDOWN how many times slower the visualizer replays
                             each move, 2 s after it (default 10; 1 = off).
                             The replay fits in the gap between moves.
  VISUALIZER_URL             default http://127.0.0.1:5000
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from lib.checkping import check_ping

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.topogen import Topogen, get_topogen
from lib.topolog import logger

pytestmark = [pytest.mark.bgpd, pytest.mark.evpn]

ANYCAST_MAC = "00:00:5e:00:01:01"

SPINES = {
    "spine1": "10.0.0.101",
    "spine2": "10.0.0.102",
}

LEAVES = {
    "leaf1": {"lo": "10.0.0.1", "l2vni": 1000, "l2br": "br1000",
              "svi": "45.0.0.1/24", "hosts": {}, "gw": "45.0.0.1"},
    "leaf2": {"lo": "10.0.0.2", "l2vni": 1000, "l2br": "br1000",
              "svi": "45.0.0.1/24", "hosts": {}, "gw": "45.0.0.1"},
    "leaf3": {"lo": "10.0.0.3", "l2vni": 1000, "l2br": "br1000",
              "svi": "45.0.0.1/24", "hosts": {}, "gw": "45.0.0.1"},
    "leaf4": {"lo": "10.0.0.4", "l2vni": 1000, "l2br": "br1000",
              "svi": "45.0.0.1/24", "hosts": {}, "gw": "45.0.0.1"},
    "leaf5": {"lo": "10.0.0.5", "l2vni": 2000, "l2br": "br2000",
              "svi": "20.0.0.1/24",
              "hosts": {"controller": ("20.0.0.51/24", "00:00:00:00:00:51")},
              "gw": "20.0.0.1"},
}
VTEP_TO_LEAF = {spec["lo"]: leaf for leaf, spec in LEAVES.items()}

L3VNI = 500
L3BR = "br500"
TENANT_VRF = "VRF500"
ROBOT_L2VNI = 1000
# Leaves that carry the robot's L2VNI; leaf5 only reaches it over the L3VNI.
L2_LEAVES = ["leaf1", "leaf2", "leaf3", "leaf4"]

ROBOT = {
    "name": "robot1",
    "ip": "45.0.0.100/24",
    "addr": "45.0.0.100",
    "mac": "00:00:00:00:01:00",
    "gw": "45.0.0.1",
    "leaves": ("leaf1", "leaf2"),
}
CONTROLLER = "controller"
CONTROLLER_ADDR = "20.0.0.51"


def _env_float(name, default):
    return float(os.environ.get(name, default))


MOVES = int(os.environ.get("MOVE_OUTAGE_MOVES", "4"))
GAP = _env_float("MOVE_OUTAGE_GAP", "15")
BUDGET = _env_float("MOVE_OUTAGE_BUDGET_MS", "100") / 1000.0
BASELINE = _env_float("MOVE_OUTAGE_BASELINE", "10")
REARP_COUNT = int(os.environ.get("MOVE_OUTAGE_REARP_COUNT", "30"))
REARP_INTERVAL = _env_float("MOVE_OUTAGE_REARP_INTERVAL", "0.1")
VXLAN_LEARNING = os.environ.get("MOVE_OUTAGE_VXLAN_LEARNING", "off").lower() == "on"
REPLAY_SLOWDOWN = _env_float("MOVE_OUTAGE_REPLAY_SLOWDOWN", "10")
# Seconds of move time replayed at most (10 s of replay at 10x), and how long
# after a move the replay starts, once its events have been recorded.
REPLAY_WINDOW = 1.0
REPLAY_AFTER = 2.0

PROBE_INTERVAL = 0.01
INSTRUCTION_EVERY = 10
TRAFFIC_PORT = 5555
TRAFFIC_SCRIPT = os.path.join(CWD, "move_outage_traffic.py")
# Helpers run under "timeout" so they exit on their own inside the namespace.
RUN_SECONDS = int(BASELINE + MOVES * GAP + 120)
# tc filter priority used for the robot's "radio off" drop filters.
RADIO_PREF = 10

VISUALIZER_URL = os.environ.get("VISUALIZER_URL", "http://127.0.0.1:5000")

# Shared between the tests in this module.
STATE = {"moves": []}


def _announce(msg):
    "Print a status line to the pytest console (needs pytest -s)."
    print("[outage] {}".format(msg), flush=True)
    logger.info(msg)


def _iface(leaf):
    "The robot's interface toward leaf."
    return "eth-{}".format(leaf)


def _text(raw):
    "topotest popen may return bytes or str lines."
    return raw.decode(errors="replace") if isinstance(raw, bytes) else raw


def _evidence_dir():
    path = os.path.join(get_topogen().logdir, "move_outage")
    os.makedirs(path, exist_ok=True)
    return path


#
# Visualizer
#


class _Viz:
    "Best-effort visualizer client; posts from a background thread."

    def __init__(self):
        self.enabled = False
        self.events = queue.Queue()
        self.thread = None

    def _post(self, event):
        req = urllib.request.Request(
            VISUALIZER_URL + "/event",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=1).close()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _run(self):
        while True:
            event = self.events.get()
            if event is None:
                return
            self._post(event)

    def start(self):
        if not self._post({"action": "RESET"}):
            logger.info("Visualizer not reachable at %s, skipping", VISUALIZER_URL)
            return False
        self.enabled = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def send(self, event):
        if self.enabled:
            self.events.put(event)

    def log(self, text, level="info"):
        self.send({"action": "LOG", "text": text, "level": level})

    def stop(self):
        if self.thread:
            self.events.put(None)
            self.thread.join(timeout=10)


VIZ = _Viz()


def _viz_publish_topology(tgen, start_leaf):
    "Send the fabric nodes, links and configs to the visualizer."
    for spine in SPINES:
        VIZ.send({"action": "ADD_NODE", "id": spine, "type": "spine"})
    for leaf, spec in LEAVES.items():
        VIZ.send({"action": "ADD_NODE", "id": leaf, "type": "vtep"})
        for host in spec["hosts"]:
            VIZ.send({"action": "ADD_NODE", "id": host, "type": "host"})
    VIZ.send({"action": "ADD_NODE", "id": ROBOT["name"], "type": "host"})
    for spine in SPINES:
        for leaf in LEAVES:
            VIZ.send({"action": "ADD_LINK", "source": spine, "target": leaf})
    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            VIZ.send({"action": "ADD_LINK", "source": leaf, "target": host})
    VIZ.send({"action": "ADD_LINK", "source": start_leaf, "target": ROBOT["name"]})
    for rname, router in tgen.routers().items():
        VIZ.send({"action": "NODE_CONFIG", "id": rname,
                  "config": router.vtysh_cmd("show running-config")})


#
# Kernel event monitors
#

IP_TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\] (.*)$")
BRIDGE_TS = re.compile(r"^Timestamp: \w+ (\w+ +\d+ \d\d:\d\d:\d\d \d{4}) (\d+) usec")
VIA = re.compile(r"via (\d+\.\d+\.\d+\.\d+)")
DST = re.compile(r" dst (\d+\.\d+\.\d+\.\d+)")


class _LeafWatch:
    """Follow every leaf's view of the robot from kernel netlink events.

    view[leaf] is "local", the name of the leaf it points at, or None. For
    leaf1-4 it comes from the bridge FDB entry for the robot's MAC; for leaf5
    it comes from the robot's /32 route in the tenant VRF.
    """

    def __init__(self, tgen, on_change):
        self.tgen = tgen
        self.on_change = on_change
        self.lock = threading.Lock()
        self.events = []
        self.view = {leaf: None for leaf in LEAVES}
        self.fdb = {leaf: {"port": None, "dst": None} for leaf in L2_LEAVES}
        self.procs = []

    # -- parsing --

    def _fdb_line(self, leaf, line):
        "Return an event dict for an FDB line about the robot's MAC, or None."
        if ROBOT["mac"] not in line.lower():
            return None
        deleted = line.startswith("Deleted ")
        state = self.fdb[leaf]
        robot_port = "dev eth-{} ".format(ROBOT["name"])
        if robot_port in line + " " and "master" in line:
            what = "MAC on robot port (local)"
            state["port"] = None if deleted else "local"
        elif "dev vni" in line and DST.search(line):
            vtep = DST.search(line).group(1)
            what = "MAC via VTEP {} ({})".format(vtep, VTEP_TO_LEAF.get(vtep, "?"))
            if deleted:
                if state["dst"] == vtep:
                    state["dst"] = None
            else:
                state["dst"] = vtep
        elif "dev vni" in line and "master" in line:
            if "extern_learn" in line:
                what = "MAC on VXLAN port (installed by zebra)"
                port = "vxlan"
            else:
                # The bridge learned the MAC from a frame that arrived over
                # VXLAN (the robot's flooded ARP): flood-and-learn, not EVPN.
                what = "MAC learned from flooded traffic over VXLAN (data plane, not EVPN)"
                port = "flood"
            if deleted:
                if state["port"] == port:
                    state["port"] = None
            else:
                state["port"] = port
        else:
            return None
        if state["port"] == "local":
            view = "local"
        elif state["port"] == "flood":
            view = "flood"
        elif state["dst"]:
            view = VTEP_TO_LEAF.get(state["dst"], state["dst"])
        else:
            view = None
        return {"kind": "fdb", "deleted": deleted, "what": what, "view": view}

    def _ip_line(self, leaf, line):
        "Return an event dict for a neigh/route line about the robot's IP."
        if not re.search(r"(^|\s){}(/32)?\s".format(re.escape(ROBOT["addr"])), line + " "):
            return None
        deleted = line.startswith("Deleted ")
        if " lladdr " in line or "FAILED" in line or "INCOMPLETE" in line:
            remote = "extern_learn" in line
            what = "IP neighbor {}{}".format(
                "remote (installed by zebra)" if remote else "LOCAL (learned by ARP)",
                " deleted" if deleted else "",
            )
            if "FAILED" in line or "INCOMPLETE" in line:
                what = "IP neighbor unresolved (leaf is ARPing for the robot)"
            return {"kind": "neigh", "deleted": deleted, "remote": remote,
                    "what": what, "view": None}
        if leaf != "leaf5":
            return {"kind": "route", "deleted": deleted, "what": "host route" +
                    (" deleted" if deleted else ""), "view": None}
        via = VIA.search(line)
        if deleted:
            view = None
            what = "/32 route withdrawn (falls back to the 45.0.0.0/24 subnet route)"
        elif via:
            vtep = via.group(1)
            view = VTEP_TO_LEAF.get(vtep, vtep)
            what = "/32 route via VTEP {} ({})".format(vtep, view)
        else:
            return None
        return {"kind": "route", "deleted": deleted, "what": what, "view": view,
                "via": via.group(1) if via else None}

    def _record(self, leaf, t, raw, event):
        if event is None:
            return
        event.update({"t": t, "leaf": leaf, "raw": raw})
        changed = False
        with self.lock:
            self.events.append(event)
            sets_view = event["kind"] == "fdb" or (
                leaf == "leaf5" and event["kind"] == "route")
            if sets_view and self.view[leaf] != event["view"]:
                self.view[leaf] = event["view"]
                changed = True
        if changed:
            self.on_change(leaf, event["view"])

    # -- readers --

    def _reader(self, leaf, kind, proc, path):
        last_ts = None
        with open(path, "w") as log:
            for raw in proc.stdout:
                line = _text(raw).rstrip("\n")
                log.write(line + "\n")
                log.flush()
                if kind == "ip":
                    m = IP_TS.match(line)
                    if not m:
                        continue
                    t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f").timestamp()
                    self._record(leaf, t, m.group(2), self._ip_line(leaf, m.group(2)))
                else:
                    m = BRIDGE_TS.match(line)
                    if m:
                        last_ts = time.mktime(
                            time.strptime(m.group(1), "%b %d %H:%M:%S %Y")
                        ) + int(m.group(2)) / 1e6
                        continue
                    if last_ts is not None and line.strip():
                        self._record(leaf, last_ts, line, self._fdb_line(leaf, line))

    def _spawn(self, leaf, kind, cmd):
        proc = self.tgen.net[leaf].popen(
            ["timeout", str(RUN_SECONDS)] + cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        path = os.path.join(_evidence_dir(), "{}_{}.log".format(leaf, kind))
        thread = threading.Thread(
            target=self._reader, args=(leaf, kind, proc, path), daemon=True
        )
        thread.start()
        self.procs.append(proc)

    def seed(self):
        "Read each leaf's current view of the robot before monitoring starts."
        now = time.time()
        for leaf in L2_LEAVES:
            out = self.tgen.net[leaf].cmd("bridge fdb show | grep -i {}".format(ROBOT["mac"]))
            for line in out.splitlines():
                self._record(leaf, now, line, self._fdb_line(leaf, line))
        out = self.tgen.net["leaf5"].cmd(
            "ip route show table {} {}".format(L3VNI, ROBOT["addr"])
        )
        for line in out.splitlines():
            self._record("leaf5", now, line, self._ip_line("leaf5", line))

    def start(self):
        self.seed()
        for leaf in LEAVES:
            self._spawn(leaf, "ip", ["ip", "-ts", "monitor", "neigh", "route"])
            if leaf in L2_LEAVES:
                # iproute2 5.15's bridge has -timestamp but not -ts.
                self._spawn(leaf, "fdb", ["bridge", "-timestamp", "monitor", "fdb"])
        # Give the monitors time to subscribe.
        time.sleep(0.5)

    def between(self, t_from, t_to):
        with self.lock:
            return [e for e in self.events if t_from <= e["t"] < t_to]

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()


#
# Instruction stream
#


class _Stream:
    "Read the controller's probe log live and judge each instruction."

    def __init__(self, tgen, watch):
        self.tgen = tgen
        self.watch = watch
        self.lock = threading.Lock()
        self.sent = {}
        self.recv = {}
        self.pending = []
        self.delivered = 0
        self.missed = 0
        self.proc = None
        self.thread = None

    def _judge(self, now):
        "Decide instructions whose deadline has passed, and show them."
        while self.pending and now > self.sent[self.pending[0]] + BUDGET:
            seq = self.pending.pop(0)
            rtt = self.recv.get(seq, float("inf")) - self.sent[seq]
            # A replay draws its own recorded instructions meanwhile.
            show = not STATE.get("replaying")
            if rtt <= BUDGET:
                self.delivered += 1
                if show:
                    VIZ.send({"action": "PACKET_FLOW", "packet_type": "REACH",
                              "source": CONTROLLER, "target": ROBOT["name"],
                              "vm": ROBOT["name"]})
            else:
                self.missed += 1
                # Show where leaf5 is sending the instruction instead.
                target = self.watch.view["leaf5"] or "leaf5"
                if show:
                    VIZ.send({"action": "PACKET_FLOW", "packet_type": "UNREACH",
                              "source": CONTROLLER, "target": target,
                              "vm": ROBOT["name"]})
            VIZ.send({"action": "STREAM_STATS", "delivered": self.delivered,
                      "missed": self.missed})

    def _reader(self):
        for raw in self.proc.stdout:
            parts = _text(raw).split()
            if len(parts) != 3:
                continue
            seq, t = int(parts[1]), float(parts[2])
            with self.lock:
                if parts[0] == "S":
                    self.sent[seq] = t
                    if seq % INSTRUCTION_EVERY == 0:
                        self.pending.append(seq)
                elif parts[0] == "R":
                    self.recv.setdefault(seq, t)
                self._judge(time.time())

    def start(self, duration):
        self.proc = self.tgen.net[CONTROLLER].popen(
            ["timeout", str(RUN_SECONDS), "python3", "-u", TRAFFIC_SCRIPT, "stream",
             "--target", ROBOT["addr"], "--port", str(TRAFFIC_PORT),
             "--interval", str(PROBE_INTERVAL), "--duration", str(duration)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def probes(self, t_from, t_to):
        "Return [(seq, t_send, rtt or None)] for probes sent in [t_from, t_to)."
        with self.lock:
            out = []
            for seq, t in sorted(self.sent.items()):
                if t_from <= t < t_to:
                    rtt = self.recv[seq] - t if seq in self.recv else None
                    out.append((seq, t, rtt))
            return out

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def _ok(rtt):
    return rtt is not None and rtt <= BUDGET


#
# Topology
#


def build_topo(tgen):
    "Build the Capstone EVPN fabric plus a robot cabled to leaf1 and leaf2."
    for spine in SPINES:
        tgen.add_router(spine)
    for leaf in LEAVES:
        tgen.add_router(leaf)

    for spine in SPINES:
        for leaf in LEAVES:
            tgen.add_link(tgen.gears[spine], tgen.gears[leaf],
                          "eth-{}".format(leaf), "eth-{}".format(spine))

    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            tgen.add_host(host, spec["hosts"][host][0], "via {}".format(spec["gw"]))
            tgen.add_link(tgen.gears[host], tgen.gears[leaf],
                          "eth0", "eth-{}".format(host))

    tgen.add_host(ROBOT["name"], ROBOT["ip"], "via {}".format(ROBOT["gw"]))
    for leaf in ROBOT["leaves"]:
        tgen.add_link(tgen.gears[ROBOT["name"]], tgen.gears[leaf],
                      _iface(leaf), "eth-{}".format(ROBOT["name"]))


def _setup_leaf_dataplane(tgen, leaf, spec):
    "Create VRF, L3VNI, L2VNI, SVI and enslave access ports on a leaf."
    tgen.net[leaf].cmd_raises(
        """
ip link add {vrf} type vrf table {l3vni}
ip link set {vrf} up

ip link add {l3br} type bridge stp_state 0
ip link set {l3br} master {vrf}
ip link set {l3br} up
ip link add vni{l3vni} type vxlan id {l3vni} dstport 4789 local {lo} nolearning
ip link set vni{l3vni} master {l3br}
ip link set vni{l3vni} up

ip link add {l2br} type bridge stp_state 0
ip link set {l2br} address {mac}
ip link set {l2br} master {vrf}
ip address add {svi} dev {l2br}
ip link set {l2br} up
ip link add vni{l2vni} type vxlan id {l2vni} dstport 4789 local {lo} nolearning
ip link set vni{l2vni} master {l2br}
ip link set vni{l2vni} up
""".format(vrf=TENANT_VRF, l3vni=L3VNI, l3br=L3BR, l2vni=spec["l2vni"],
           l2br=spec["l2br"], lo=spec["lo"], svi=spec["svi"], mac=ANYCAST_MAC)
    )
    for host in spec["hosts"]:
        tgen.net[leaf].cmd_raises(
            "ip link set eth-{host} master {l2br}; "
            "ip link set eth-{host} up".format(host=host, l2br=spec["l2br"])
        )


def _radio_cmd(iface, on):
    "Return the shell command that turns the robot's radio on iface on or off."
    if on:
        return ("tc filter del dev {i} ingress pref {p} && "
                "tc filter del dev {i} egress pref {p}").format(i=iface, p=RADIO_PREF)
    return ("tc filter add dev {i} ingress pref {p} matchall action drop && "
            "tc filter add dev {i} egress pref {p} matchall action drop"
            ).format(i=iface, p=RADIO_PREF)


def _setup_robot(tgen):
    "Bridge both robot links; radio on toward the first leaf only."
    home, other = ROBOT["leaves"]
    robot = tgen.net[ROBOT["name"]]
    for leaf in ROBOT["leaves"]:
        robot.cmd_raises("ip link set {} address {}".format(_iface(leaf), ROBOT["mac"]))
        robot.cmd_raises("tc qdisc add dev {} clsact".format(_iface(leaf)))
    # Radio off on the second link before anything is sent on it.
    robot.cmd_raises(_radio_cmd(_iface(other), False))
    robot.cmd_raises("ip addr flush dev {}".format(_iface(other)))
    robot.cmd_raises("ip addr replace {} dev {}".format(ROBOT["ip"], _iface(home)))
    robot.cmd_raises("ip route replace default via {}".format(ROBOT["gw"]))
    for leaf in ROBOT["leaves"]:
        robot.cmd_raises("ip link set {} up".format(_iface(leaf)))
        # Both "access point" ports are always up and bridged.
        tgen.net[leaf].cmd_raises(
            "ip link set eth-{} master {}; ip link set eth-{} up".format(
                ROBOT["name"], LEAVES[leaf]["l2br"], ROBOT["name"])
        )


def setup_module(mod):
    "Sets up the pytest environment"
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    for leaf, spec in LEAVES.items():
        logger.info("Programming EVPN dataplane on %s", leaf)
        _setup_leaf_dataplane(tgen, leaf, spec)
    for spec in LEAVES.values():
        for host in spec["hosts"]:
            tgen.net[host].cmd_raises(
                "ip link set dev eth0 address {}".format(spec["hosts"][host][1])
            )
    _setup_robot(tgen)
    if not VXLAN_LEARNING:
        # EVPN only: the bridges stop learning MACs from frames that arrive
        # over VXLAN, so only BGP can move the robot.
        for leaf in L2_LEAVES:
            tgen.net[leaf].cmd_raises(
                "bridge link set dev vni{} learning off".format(LEAVES[leaf]["l2vni"])
            )

    for rname, router in tgen.routers().items():
        logger.info("Loading router %s", rname)
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))
    tgen.start_router()
    _announce("Topology up; logdir={}; VXLAN data-plane learning {}".format(
        tgen.logdir, "on" if VXLAN_LEARNING else "off (EVPN only)"))


def teardown_module(mod):
    "Teardown the pytest environment"
    for name in ("stream", "watch"):
        if name in STATE:
            STATE[name].stop()
    for name in ("echo", "agent"):
        if name in STATE and STATE[name].poll() is None:
            STATE[name].kill()
    VIZ.stop()
    get_topogen().stop_topology()


#
# EVPN state helpers
#


def _peers_established(router, afi, expected_peers):
    "Return None when all expected peers are Established, else a message."
    output = router.vtysh_cmd("show bgp {} summary json".format(afi), isjson=True)
    peers = output.get("peers", {})
    for peer in expected_peers:
        state = peers.get(peer, {}).get("state")
        if state != "Established":
            return "{} {} peer {} state {}".format(router.name, afi, peer, state)
    return None


def _wait_for_evpn_fabric(tgen):
    "Block until underlay, EVPN sessions, and VNIs are present."
    expected = {spine: ["eth-{}".format(leaf) for leaf in LEAVES] for spine in SPINES}
    expected.update({leaf: ["eth-{}".format(s) for s in SPINES] for leaf in LEAVES})
    for rname, peers in expected.items():
        router = tgen.gears[rname]
        for afi in ("ipv4 unicast", "l2vpn evpn"):
            _, result = topotest.run_and_expect(
                lambda r=router, a=afi, p=peers: _peers_established(r, a, p),
                None, count=90, wait=1,
            )
            assert result is None, "BGP did not converge: {}".format(result)

    for leaf, spec in LEAVES.items():
        router = tgen.gears[leaf]
        wanted = {str(L3VNI), str(spec["l2vni"])}

        def _check(router=router, wanted=wanted):
            missing = wanted - set(router.vtysh_cmd("show evpn vni json", isjson=True))
            return "{} missing VNIs {}".format(router.name, sorted(missing)) if missing else None

        _, result = topotest.run_and_expect(_check, None, count=60, wait=1)
        assert result is None, result


def _robot_mac_info(router):
    "Return the EVPN MAC JSON object for the robot, or None if missing."
    output = router.vtysh_cmd(
        "show evpn mac vni {} mac {} json".format(ROBOT_L2VNI, ROBOT["mac"]), isjson=True
    )
    info = (output or {}).get(ROBOT["mac"], output)
    if not isinstance(info, dict) or "type" not in info:
        return None
    return info


def _robot_at(tgen, leaf):
    "Return None when leaf owns the robot and leaf5 routes to it, else a message."
    info = _robot_mac_info(tgen.gears[leaf])
    if not info or info.get("type") != "local":
        return "{} does not have the robot MAC as local: {}".format(leaf, info)
    vtep = LEAVES[leaf]["lo"]
    route = tgen.gears["leaf5"].vtysh_cmd(
        "show ip route vrf {} {}/32 json".format(TENANT_VRF, ROBOT["addr"]), isjson=True
    )
    if vtep not in json.dumps(route):
        return "leaf5 has no /32 for the robot via {}".format(vtep)
    return None


def _evpn_snapshot(tgen):
    "Return text and a dict of every leaf's EVPN state for the robot."
    lines, state = [], {}
    for leaf in L2_LEAVES:
        info = _robot_mac_info(tgen.gears[leaf]) or {}
        state[leaf] = {
            "type": info.get("type"),
            "remoteVtep": info.get("remoteVtep"),
            "localSequence": info.get("localSequence"),
            "remoteSequence": info.get("remoteSequence"),
            "isDuplicate": info.get("isDuplicate"),
        }
        lines.append("{}: {}".format(leaf, state[leaf]))
    route = tgen.gears["leaf5"].vtysh_cmd(
        "show ip route vrf {} {}".format(TENANT_VRF, ROBOT["addr"])
    )
    lines.append("leaf5: show ip route vrf {} {}\n{}".format(TENANT_VRF, ROBOT["addr"], route))
    for leaf in ROBOT["leaves"]:
        lines.append("{}: {}".format(
            leaf, tgen.gears[leaf].vtysh_cmd("show bgp l2vpn evpn route type macip")
            .count(ROBOT["mac"])) + " RT-2 paths for the robot MAC in BGP")
    return "\n".join(lines), state


#
# Moves and analysis
#


def _show_leaf(leaf, view, where):
    "Colour a leaf in the visualizer by whether it knows where the robot is."
    if view is None:
        status, note = "none", "no entry"
    elif view == "local":
        status = "ok" if leaf == where else "stale"
        note = "local"
    elif view == "flood":
        status, note = "flood", "flooding (data plane)"
    else:
        status = "ok" if view == where else "stale"
        note = "-> {}".format(view)
    VIZ.send({"action": "NODE_STATUS", "id": leaf, "status": status, "note": note})


def _on_view_change(leaf, view):
    "Live view changes; held back while a replay owns the leaf colours."
    if not STATE.get("replaying"):
        _show_leaf(leaf, view, STATE.get("robot_at"))


def _refresh_views():
    for leaf, view in STATE["watch"].view.items():
        _on_view_change(leaf, view)


def _replay(no, old_leaf, new_leaf, t_move, t_on, leaf5_before, events, probes):
    """Replay one move in the visualizer, REPLAY_SLOWDOWN times slower.

    The move itself ran at full speed; this re-plays the recorded kernel
    events and instruction results so the ~100 ms stale window is visible.
    Yellow dots are EVPN updates leaving the new leaf through the spines.
    """
    if not VIZ.enabled or REPLAY_SLOWDOWN <= 1:
        return
    spines = list(SPINES)
    actions = [(t_on - t_move, "radio", None)]
    for e in events:
        rel = e["t"] - t_move
        if e["kind"] == "fdb":
            actions.append((rel, "view", (e["leaf"], e["view"])))
            if e["leaf"] == new_leaf and e["view"] == "local":
                for spine in spines:
                    actions.append((rel, "update", (new_leaf, spine)))
            elif e["view"] == new_leaf:
                actions.append((rel, "update", (spines[0], e["leaf"])))
        elif e["kind"] == "route" and e["leaf"] == "leaf5":
            actions.append((rel, "view", ("leaf5", e["view"])))
            actions.append((rel, "update", (spines[1], "leaf5")))
    for t, leaf, what in _chain(events, old_leaf, new_leaf):
        actions.append((t - t_move, "log", "   {:+.0f} ms  {} {}".format(
            (t - t_move) * 1000, leaf, what)))
    for seq, t, rtt in probes:
        if seq % INSTRUCTION_EVERY == 0:
            actions.append((t - t_move, "instr", _ok(rtt)))
    actions.sort(key=lambda a: a[0])

    # Replay until the last leaf has moved, but never more than REPLAY_WINDOW.
    last = max([a[0] for a in actions if a[1] in ("view", "radio")] or [0.0])
    end = min(last + 0.15, REPLAY_WINDOW)

    STATE["replaying"] = True
    view = {leaf: old_leaf for leaf in LEAVES}
    view[old_leaf] = "local"
    view["leaf5"] = leaf5_before
    for leaf in LEAVES:
        _show_leaf(leaf, view[leaf], new_leaf)
    VIZ.send({"action": "ADD_LINK", "source": old_leaf, "target": ROBOT["name"]})
    VIZ.send({"action": "DEL_LINK", "source": new_leaf, "target": ROBOT["name"]})
    VIZ.log("Slow-motion replay of move {} ({:.0f}x slower):".format(no, REPLAY_SLOWDOWN), "warn")

    tick, t, i = 0.01, -0.05, 0
    start = time.time()
    while t <= end:
        while i < len(actions) and actions[i][0] <= t:
            _, kind, arg = actions[i]
            i += 1
            if kind == "radio":
                VIZ.send({"action": "DEL_LINK", "source": old_leaf, "target": ROBOT["name"]})
                VIZ.send({"action": "ADD_LINK", "source": new_leaf, "target": ROBOT["name"]})
            elif kind == "view":
                view[arg[0]] = arg[1]
                _show_leaf(arg[0], arg[1], new_leaf)
            elif kind == "update":
                VIZ.send({"action": "PACKET_FLOW", "packet_type": "UPDATE",
                          "source": arg[0], "target": arg[1], "vm": "EVPN update"})
            elif kind == "log":
                VIZ.log(arg)
            elif arg:
                VIZ.send({"action": "PACKET_FLOW", "packet_type": "REACH",
                          "source": CONTROLLER, "target": ROBOT["name"],
                          "vm": ROBOT["name"]})
            else:
                VIZ.send({"action": "PACKET_FLOW", "packet_type": "UNREACH",
                          "source": CONTROLLER, "target": view["leaf5"] or "leaf5",
                          "vm": ROBOT["name"]})
        VIZ.send({"action": "REPLAY_CLOCK",
                  "text": "Slow motion {:.0f}x - move {} {} -> {}: {:+.0f} ms".format(
                      REPLAY_SLOWDOWN, no, old_leaf, new_leaf, t * 1000)})
        t += tick
        time.sleep(max(0.0, start + (t + 0.05) * REPLAY_SLOWDOWN - time.time()))

    VIZ.send({"action": "REPLAY_CLOCK", "text": ""})
    STATE["replaying"] = False
    VIZ.send({"action": "DEL_LINK", "source": old_leaf, "target": ROBOT["name"]})
    VIZ.send({"action": "ADD_LINK", "source": new_leaf, "target": ROBOT["name"]})
    _refresh_views()


def _start_agent(tgen):
    "Start the robot-side roam agent (see move_outage_traffic.py)."
    return tgen.net[ROBOT["name"]].popen(
        ["timeout", str(RUN_SECONDS), "python3", "-u", TRAFFIC_SCRIPT, "agent",
         "--radio-pref", str(RADIO_PREF)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _roam(tgen, old_leaf, new_leaf):
    """Move the robot's radio from old_leaf to new_leaf.

    Return (t_off, t_on, t_arp): when the robot's traffic left the old link,
    when its radio came on at the new one, and when it sent its first ARP
    there. Everything after t_on is the fabric's doing.
    """
    agent = STATE["agent"]
    line = "roam {} {} {} {} {} {}\n".format(
        _iface(old_leaf), _iface(new_leaf), ROBOT["ip"], ROBOT["gw"],
        REARP_COUNT, REARP_INTERVAL)
    try:
        agent.stdin.write(line)
    except TypeError:
        agent.stdin.write(line.encode())
    agent.stdin.flush()
    while True:
        reply = _text(agent.stdout.readline())
        assert reply, "robot roam agent exited"
        if reply.startswith("T "):
            return tuple(float(v) for v in reply.split()[1:4])
        logger.info("roam agent: %s", reply.rstrip())


def _first(events, leaf, test):
    for e in events:
        if e["leaf"] == leaf and test(e):
            return e
    return None


def _chain(events, old_leaf, new_leaf):
    "Return [(t, leaf, what)] for the key steps of one move, in time order."
    new_vtep = LEAVES[new_leaf]["lo"]
    rules = [
        (new_leaf, "learned the robot MAC on its access port",
         lambda e: e["kind"] == "fdb" and e["view"] == "local"),
        (new_leaf, "learned the robot IP from ARP (local neighbor)",
         lambda e: e["kind"] == "neigh" and not e["remote"] and not e["deleted"]
         and " lladdr " in e["raw"]),
        (old_leaf, "points the robot MAC at {}".format(new_leaf),
         lambda e: e["kind"] == "fdb" and e["view"] == new_leaf),
        (old_leaf, "floods the robot's traffic over VXLAN (learned from the robot's "
                   "ARP broadcast, not from EVPN)",
         lambda e: e["kind"] == "fdb" and e["view"] == "flood"),
        (old_leaf, "replaced its local IP neighbor with zebra's remote copy",
         lambda e: e["kind"] == "neigh" and e["remote"] and not e["deleted"]),
    ]
    for leaf in L2_LEAVES:
        if leaf not in (old_leaf, new_leaf):
            rules.append((leaf, "points the robot MAC at {}".format(new_leaf),
                          lambda e: e["kind"] == "fdb" and e["view"] == new_leaf))
    rules += [
        ("leaf5", "withdrew the robot /32 (falls back to the subnet route)",
         lambda e: e["kind"] == "route" and e["deleted"]),
        ("leaf5", "routes the robot /32 to {}".format(new_leaf),
         lambda e: e["kind"] == "route" and not e["deleted"] and e.get("via") == new_vtep),
    ]
    steps = []
    for leaf, what, test in rules:
        e = _first(events, leaf, test)
        if e:
            steps.append((e["t"], leaf, what))
    return sorted(steps)


def _path_fixes(move):
    """Return [(t, how)] for each step that put the controller's traffic back
    on a path to the robot: leaf5's /32 moving to the new leaf, or the old
    leaf sending the robot's traffic on to it (by EVPN or by flooding)."""
    fixes = []
    for t, leaf, what in move["chain"]:
        if leaf == "leaf5" and what.startswith("routes"):
            fixes.append((t, "leaf5 route"))
        elif leaf == move["old"] and what.startswith("points"):
            fixes.append((t, "{} EVPN".format(leaf)))
        elif leaf == move["old"] and what.startswith("floods"):
            fixes.append((t, "{} flooding".format(leaf)))
    return fixes


def _analyze(probes, t_move):
    "Return the outage numbers for one move window."
    failed = [(seq, t) for seq, t, rtt in probes if not _ok(rtt)]
    instr = [(seq, t, rtt) for seq, t, rtt in probes if seq % INSTRUCTION_EVERY == 0]
    result = {
        "probes": len(probes),
        "failed_probes": len(failed),
        "instructions": len(instr),
        "missed_instructions": sum(1 for _, _, rtt in instr if not _ok(rtt)),
        "outage_start": None,
        "recovered_at": None,
        "outage_ms": None,
        "recovered": True,
    }
    if not failed:
        result["outage_ms"] = 0.0
        return result
    t_fail = failed[0][1]
    result["outage_start"] = t_fail
    for _, t, rtt in probes:
        if t > t_fail and _ok(rtt):
            result["recovered_at"] = t
            result["outage_ms"] = (t - t_fail) * 1000
            return result
    result["recovered"] = False
    result["outage_ms"] = (probes[-1][1] - t_fail) * 1000 if probes else None
    return result


def _ms(t, t0):
    return "{:+8.1f} ms".format((t - t0) * 1000)


def _write_timeline(tgen, move, events, probes, snapshot_text):
    "Write one move's evidence file and return its path."
    t0 = move["t_move"]
    lines = [
        "Move {no}: robot {old} -> {new}".format(**move),
        "Radio switched at {} ({:.6f}); times below are relative to it.".format(
            datetime.fromtimestamp(t0).strftime("%H:%M:%S.%f"), t0),
        "Instruction budget {:.0f} ms, one instruction every {:.0f} ms.".format(
            BUDGET * 1000, PROBE_INTERVAL * INSTRUCTION_EVERY * 1000),
        "",
        "== Update chain",
    ]
    steps = list(move["chain"])
    steps.append((move["t_on"], ROBOT["name"], "radio on at the new link (robot finished switching)"))
    steps.append((move["t_arp"], ROBOT["name"], "sent ARP for its gateway (announces itself)"))
    if move["recovered_at"]:
        steps.append((move["recovered_at"], CONTROLLER, "first probe delivered again"))
    for t, leaf, what in sorted(steps):
        lines.append("  {}  {:10} {}".format(_ms(t, t0), leaf, what))
    lines += [
        "",
        "== Result",
        "  outage {} ms ({} ms of it after the robot was back on air), "
        "{} of {} instructions missed, {} of {} probes failed{}".format(
            "{:.1f}".format(move["outage_ms"]) if move["outage_ms"] is not None else "-",
            "{:.1f}".format(move["fabric_outage_ms"])
            if move["fabric_outage_ms"] is not None else "-",
            move["missed_instructions"], move["instructions"],
            move["failed_probes"], move["probes"],
            "" if move["recovered"] else " -- NEVER RECOVERED before the next move"),
        "  " + ("leaf5 routed the robot's /32 to {} when the move started.".format(move["old"])
                if move["measures_evpn"] else
                "leaf5 had no /32 to {} when the move started (it was {}), so traffic "
                "already used the subnet route: this move does not measure EVPN.".format(
                    move["old"], move["leaf5_before"])),
        "",
        "== Instructions (every {}th probe)".format(INSTRUCTION_EVERY),
    ]
    for seq, t, rtt in probes:
        if seq % INSTRUCTION_EVERY:
            continue
        if t - t0 > 3 and _ok(rtt) and move["recovered"]:
            continue
        lines.append("  {}  #{:<6} {}".format(
            _ms(t, t0), seq,
            "delivered, rtt {:.1f} ms".format(rtt * 1000) if _ok(rtt) else
            ("LATE, rtt {:.1f} ms".format(rtt * 1000) if rtt is not None else "MISSED")))
    lines += ["", "== Kernel events about the robot (ip/bridge monitor)"]
    for e in events:
        lines.append("  {}  {:6} {:28} | {}".format(_ms(e["t"], t0), e["leaf"], e["what"][:28], e["raw"]))
    lines += ["", "== EVPN state at the end of the move window", snapshot_text]
    path = os.path.join(_evidence_dir(), "move{}_timeline.txt".format(move["no"]))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _viz_move_summary(move):
    t0 = move["t_move"]
    head = "Move {no} {old} -> {new}: ".format(**move)
    if not move["recovered"]:
        VIZ.log(head + "robot NEVER reachable again ({} instructions missed)".format(
            move["missed_instructions"]), "bad")
    else:
        VIZ.log(head + "outage {:.0f} ms after the robot was back on air, {} instructions "
                "missed".format(move["fabric_outage_ms"], move["missed_instructions"]),
                "bad" if move["fabric_outage_ms"] > BUDGET * 1000 else "good")
    if REPLAY_SLOWDOWN <= 1:
        # Otherwise the replay has already logged the chain.
        for t, leaf, what in move["chain"]:
            VIZ.log("   {:+.0f} ms  {} {}".format((t - t0) * 1000, leaf, what))
    if move["recovered_at"]:
        VIZ.log("   {:+.0f} ms  instructions delivered again".format(
            (move["recovered_at"] - t0) * 1000), "good")


#
# Tests
#


def test_fabric_converged():
    "The fabric is up, the robot is on leaf1, and the monitors are running."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    _wait_for_evpn_fabric(tgen)
    tgen.net[CONTROLLER].cmd("ping -c 2 -W 1 {}".format(LEAVES["leaf5"]["gw"]))
    home = ROBOT["leaves"][0]
    _, result = topotest.run_and_expect(
        lambda: (tgen.net[ROBOT["name"]].cmd("ping -c 1 -W 1 {}".format(ROBOT["gw"]))
                 and _robot_at(tgen, home)),
        None, count=60, wait=1,
    )
    assert result is None, result
    check_ping(CONTROLLER, ROBOT["addr"], True, count=30, wait=1)
    STATE["robot_at"] = home
    _announce("Fabric up; robot {} on {}".format(ROBOT["name"], home))

    if VIZ.start():
        _viz_publish_topology(tgen, home)
        VIZ.log("Robot on {}. Leaves show where they think the robot is: "
                "blue = correct, orange = stale (old leaf).".format(home))
        _announce("Publishing to visualizer at {}".format(VISUALIZER_URL))

    STATE["watch"] = _LeafWatch(tgen, _on_view_change)
    STATE["watch"].start()
    _refresh_views()

    STATE["echo"] = tgen.net[ROBOT["name"]].popen(
        ["timeout", str(RUN_SECONDS), "python3", TRAFFIC_SCRIPT, "echo",
         "--port", str(TRAFFIC_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    STATE["agent"] = _start_agent(tgen)
    time.sleep(0.5)


def test_baseline_no_moves():
    "With the robot standing still, no instruction is missed."
    tgen = get_topogen()
    if "watch" not in STATE:
        pytest.skip("fabric did not converge")

    stream = _Stream(tgen, STATE["watch"])
    STATE["stream"] = stream
    t_start = time.time()
    stream.start(BASELINE + MOVES * GAP + 5)
    VIZ.log("Baseline: {:.0f} s of instructions with the robot standing still".format(BASELINE))
    time.sleep(BASELINE)

    # Skip the first second while the stream starts up.
    probes = stream.probes(t_start + 1, time.time() - 1)
    result = _analyze(probes, t_start)
    rtts = sorted(rtt for _, _, rtt in probes if rtt is not None)
    p99 = rtts[int(len(rtts) * 0.99)] * 1000 if rtts else None
    STATE["baseline"] = dict(result, p99_ms=p99)
    _announce("Baseline: {} instructions, {} missed; {} of {} probes failed; p99 rtt {} ms".format(
        result["instructions"], result["missed_instructions"], result["failed_probes"],
        result["probes"], "{:.2f}".format(p99) if p99 is not None else "-"))
    VIZ.log("Baseline: {} of {} instructions missed".format(
        result["missed_instructions"], result["instructions"]),
        "good" if not result["missed_instructions"] else "bad")
    assert result["probes"] > 0, "No probes were sent"
    assert result["missed_instructions"] == 0, (
        "Instructions were missed with the robot standing still, so the fabric "
        "itself is lossy and move outages cannot be measured: {}".format(result))


def test_move_outage():
    "Every move loses instructions, until the control plane moves the robot's path."
    tgen = get_topogen()
    if "stream" not in STATE:
        pytest.skip("baseline did not run")
    watch, stream = STATE["watch"], STATE["stream"]

    for no in range(1, MOVES + 1):
        old_leaf = STATE["robot_at"]
        new_leaf = [l for l in ROBOT["leaves"] if l != old_leaf][0]
        STATE["robot_at"] = new_leaf
        VIZ.send({"action": "ADD_LINK", "source": new_leaf, "target": ROBOT["name"]})
        VIZ.send({"action": "DEL_LINK", "source": old_leaf, "target": ROBOT["name"]})
        VIZ.log("Move {}: robot roams {} -> {}".format(no, old_leaf, new_leaf), "warn")
        _refresh_views()

        # A move only measures EVPN if leaf5 routes the robot's /32 when it
        # starts; otherwise traffic is already on the subnet route.
        leaf5_before = watch.view["leaf5"]
        t_move, t_on, t_arp = _roam(tgen, old_leaf, new_leaf)
        _announce("Move {}: {} -> {} at {:.6f} (robot switched links in {:.1f} ms)".format(
            no, old_leaf, new_leaf, t_move, (t_on - t_move) * 1000))

        # Let the move play out, replay it slowly in the visualizer, snapshot
        # EVPN (vtysh) near the end of the window, away from the move itself,
        # then wait for the window to close.
        time.sleep(REPLAY_AFTER)
        now = time.time()
        _replay(no, old_leaf, new_leaf, t_move, t_on, leaf5_before,
                watch.between(t_move - 0.05, now),
                stream.probes(t_move - PROBE_INTERVAL, now))
        time.sleep(max(0.0, t_move + GAP - 3 - time.time()))
        snapshot_text, snapshot = _evpn_snapshot(tgen)
        time.sleep(max(0.0, t_move + GAP - time.time()))
        t_end = time.time()

        events = watch.between(t_move - 0.05, t_end)
        # Include a probe sent just before the switch: it was in flight.
        probes = stream.probes(t_move - PROBE_INTERVAL, t_end)
        move = {"no": no, "old": old_leaf, "new": new_leaf, "t_move": t_move,
                "chain": _chain(events, old_leaf, new_leaf), "evpn": snapshot}
        move.update(_analyze(probes, t_move))
        move["t_on"] = t_on
        move["t_arp"] = t_arp
        move["robot_switch_ms"] = (t_on - t_move) * 1000
        # The fabric's share: from the robot being back on air to delivery.
        move["fabric_outage_ms"] = (
            max(0.0, (move["recovered_at"] - t_on) * 1000) if move["recovered_at"] else None)
        move["leaf5_before"] = leaf5_before
        move["measures_evpn"] = leaf5_before == old_leaf
        move["timeline"] = _write_timeline(tgen, move, events, probes, snapshot_text)
        STATE["moves"].append(move)

        _announce("Move {}: {}".format(no, "outage {:.1f} ms, {} instructions missed{}".format(
            move["outage_ms"], move["missed_instructions"],
            "" if move["recovered"] else ", NEVER RECOVERED")))
        for t, leaf, what in move["chain"]:
            _announce("   {}  {:6} {}".format(_ms(t, t_move), leaf, what))
        _viz_move_summary(move)

    _save_results(tgen)

    for move in STATE["moves"]:
        assert move["probes"] > 0, "Move {}: no probes were sent".format(move["no"])
        if not move["measures_evpn"] or not move["recovered"]:
            continue
        assert move["missed_instructions"] > 0 or move["failed_probes"] > 0, (
            "Move {} lost nothing; the outage was not reproduced".format(move["no"]))
        # Some leaf on the controller's path (leaf5's route, or the old leaf's
        # MAC entry) must have moved the robot...
        fixes = [t for t, _ in _path_fixes(move)]
        assert fixes, (
            "Move {}: traffic recovered but no leaf on the path was seen moving "
            "the robot; see {}".format(move["no"], move["timeline"]))
        # ...and the outage must not end before the update reached the fabric.
        # It reaches every leaf within a few ms, but one leaf's monitor can
        # print late on a busy machine, so take the first leaf that saw it.
        arrived = min([t for t, leaf, what in move["chain"]
                       if leaf != move["new"] and what.startswith("points")] + fixes)
        assert move["recovered_at"] >= arrived - 2 * PROBE_INTERVAL, (
            "Move {}: traffic recovered {:.1f} ms before the EVPN update reached any "
            "leaf; see {}".format(move["no"], (arrived - move["recovered_at"]) * 1000,
                                  move["timeline"]))


def _save_results(tgen):
    path = os.path.join(_evidence_dir(), "results.json")
    data = {
        "settings": {"moves": MOVES, "gap_s": GAP, "budget_ms": BUDGET * 1000,
                     "probe_interval_s": PROBE_INTERVAL,
                     "instruction_every": INSTRUCTION_EVERY,
                     "rearp_count": REARP_COUNT, "rearp_interval_s": REARP_INTERVAL,
                     "vxlan_learning": VXLAN_LEARNING},
        "baseline": STATE.get("baseline"),
        "moves": STATE["moves"],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    _announce("Results and per-move timelines written to {}".format(_evidence_dir()))


def test_outage_verdict():
    "Print the move table; the worst fabric outage is over the budget."
    if not STATE["moves"]:
        pytest.skip("no moves ran")
    lines = ["  move path          robot_ms fabric_ms missed  path fixed by          counted"]
    for m in STATE["moves"]:
        fixes = _path_fixes(m)
        fix = "{} at {:+.0f} ms".format(min(fixes)[1], (min(fixes)[0] - m["t_move"]) * 1000) \
            if fixes else "-"
        if not m["recovered"]:
            counted = "no: never recovered"
        elif not m["measures_evpn"]:
            counted = "no: leaf5 had no /32"
        else:
            counted = "yes"
        lines.append("  {:>4} {:14} {:>8.1f} {:>9} {:>6}  {:22} {}".format(
            m["no"], "{}->{}".format(m["old"], m["new"]), m["robot_switch_ms"],
            "{:.1f}".format(m["fabric_outage_ms"]) if m["fabric_outage_ms"] is not None else "-",
            m["missed_instructions"], fix, counted))
    _announce("Move outage vs {:.0f} ms budget (robot_ms: the robot switching links; "
              "fabric_ms: the rest)\n{}".format(BUDGET * 1000, "\n".join(lines)))

    counted = [m for m in STATE["moves"] if m["recovered"] and m["measures_evpn"]]
    skipped = [m["no"] for m in STATE["moves"] if m not in counted]
    if skipped:
        _announce("Moves {} not counted: the robot's IP was lost (the IP-relearn problem), "
                  "see their timelines".format(skipped))
    assert counted, "No move could be measured; see the move timelines"
    worst = max(m["fabric_outage_ms"] for m in counted)
    best = min(m["fabric_outage_ms"] for m in counted)
    verdict = "Fabric outage per move: {:.0f}-{:.0f} ms against a {:.0f} ms budget ({})".format(
        best, worst, BUDGET * 1000,
        "VXLAN data-plane learning on" if VXLAN_LEARNING else "EVPN only")
    _announce(verdict)
    VIZ.log(verdict, "bad" if worst > BUDGET * 1000 else "good")
    assert worst > BUDGET * 1000, (
        "Problem not reproduced: every measured move stayed within the budget")


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
