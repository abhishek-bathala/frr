#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_break_before_make.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
test_evpn_capstone_break_before_make.py: Collect data on a "break before
make" robot move: the robot drops its connection to leaf1, stays offline for
a gap, then connects to leaf2.

The fabric matches project_topology.pdf (same FRR configs as
test_evpn_capstone.py). The robot is cabled to leaf1 and leaf2. Both leaf
access ports stay up and bridged; the robot's own interfaces model its radio:

  * break: the robot's link to leaf1 goes down, so leaf1 sees the cable
    unplug, flushes the robot's MAC and EVPN withdraws it.
  * gap:   the robot is connected to neither leaf.
  * make:  the robot's link to leaf2 comes up and it sends one ARP for its
    gateway straight away, so leaf2 learns its MAC and IP and advertises them.

A switch agent already running on the robot does this with one "ip -batch"
call and sends the ARP itself, so a 0 s gap takes only a few milliseconds:
the robot reaches leaf2 before leaf1's EVPN withdraw does, and EVPN treats it
as a move (the MAC-mobility sequence number goes up).

Each trial moves the robot leaf1 -> leaf2 with one gap length, then puts it
back on leaf1 (not measured) for the next trial. By default every trial uses
a 0 s gap and the test runs 10 of them, so the summary shows how consistent
the move is (min, median and max of each measurement per gap).

What each trial records:

  * Instructions: the controller sends a numbered UDP probe to the robot every
    10 ms and the robot echoes it back. Every 10th probe is an instruction
    (one per 100 ms); it is missed if its round trip is lost or takes longer
    than the budget (100 ms by default).
  * The control plane: "ip -ts monitor" and "bridge -timestamp monitor" run on
    every leaf for the whole test, so each leaf's change about the robot (MAC,
    IP, /32 route) is timestamped by the kernel. No vtysh polling runs during
    a trial.
  * EVPN state at the end of the trial: MAC-mobility sequence numbers and the
    duplicate-address flag on each leaf. The flags are not cleared between
    trials, so repeated moves show up if they trip FRR's limit.

Evidence, written to <logdir>/break_before_make/:
  trial<N>_gap<G>s.txt   timeline of the trial, missed instructions, raw
                         kernel events and EVPN state
  <leaf>_ip.log, <leaf>_fdb.log   raw monitor output for the whole run
  results.json           all measurements
  summary.txt            the per-trial table and min/median/max per gap

The test collects data; it only fails if the fabric itself is broken (lost
instructions while the robot stands still) or no probes were sent.

Environment variables:
  BBM_GAPS        comma-separated gap lengths in seconds (default 0: the
                  robot connects to leaf2 as soon as it has dropped leaf1)
  BBM_REPEATS     trials per gap (default 10, to see how consistent it is)
  BBM_WINDOW      seconds observed after the robot connects to leaf2 (default 8)
  BBM_BUDGET_MS   instruction deadline in ms (default 100)
  BBM_BASELINE    seconds of instructions with no moves (default 10)
"""

import json
import os
import queue
import re
import shutil
import statistics
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

SPINES = ["spine1", "spine2"]

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
# Leaves that carry the robot's L2VNI; leaf5 only reaches it over the L3VNI.
L2_LEAVES = ["leaf1", "leaf2", "leaf3", "leaf4"]

L3VNI = 500
L3BR = "br500"
TENANT_VRF = "VRF500"
ROBOT_L2VNI = 1000

ROBOT = {
    "name": "robot1",
    "ip": "45.0.0.100/24",
    "addr": "45.0.0.100",
    "mac": "00:00:00:00:01:00",
    "gw": "45.0.0.1",
    "from": "leaf1",
    "to": "leaf2",
}
CONTROLLER = "controller"


def _env_float(name, default):
    return float(os.environ.get(name, default))


GAPS = [float(g) for g in os.environ.get("BBM_GAPS", "0").split(",")]
REPEATS = int(os.environ.get("BBM_REPEATS", "10"))
WINDOW = _env_float("BBM_WINDOW", "8")
BUDGET = _env_float("BBM_BUDGET_MS", "100") / 1000.0
BASELINE = _env_float("BBM_BASELINE", "10")

PROBE_INTERVAL = 0.01
INSTRUCTION_EVERY = 10
TRAFFIC_PORT = 5555
TRAFFIC_SCRIPT = os.path.join(CWD, "robot_traffic.py")
# Helpers run under "timeout" so they exit on their own inside the namespace.
RUN_SECONDS = 1800

VISUALIZER_URL = os.environ.get("VISUALIZER_URL", "http://127.0.0.1:5000")

# Shared between the tests in this module.
STATE = {"trials": []}


def _announce(msg):
    "Print a status line to the pytest console (needs pytest -s)."
    print("[bbm] {}".format(msg), flush=True)
    logger.info(msg)


def _text(raw):
    "topotest popen may return bytes or str lines."
    return raw.decode(errors="replace") if isinstance(raw, bytes) else raw


def _evidence_dir():
    path = os.path.join(get_topogen().logdir, "break_before_make")
    os.makedirs(path, exist_ok=True)
    return path


def _ok(rtt):
    return rtt is not None and rtt <= BUDGET


def _iface(leaf):
    "The robot's interface toward leaf."
    return "eth-{}".format(leaf)


#
# Visualizer
#


class _Viz:
    "Best-effort visualizer client; posts from a background thread."

    def __init__(self):
        self.enabled = False
        self.events = queue.Queue()

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
            self._post(self.events.get())

    def start(self):
        if not self._post({"action": "RESET"}):
            logger.info("Visualizer not reachable at %s, skipping", VISUALIZER_URL)
            return False
        self.enabled = True
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def send(self, event):
        if self.enabled:
            self.events.put(event)

    def log(self, msg, level="info"):
        self.send({"action": "LOG", "text": msg, "level": level})

    def link(self, leaf, up):
        self.send({"action": "ADD_LINK" if up else "DEL_LINK",
                   "source": leaf, "target": ROBOT["name"]})


VIZ = _Viz()


def _viz_publish_topology(tgen):
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
    VIZ.link(ROBOT["from"], True)
    for rname, router in tgen.routers().items():
        VIZ.send({"action": "NODE_CONFIG", "id": rname,
                  "config": router.vtysh_cmd("show running-config")})


def _show_leaf(leaf):
    """Label a leaf with where it thinks the robot is: green-blue when right,
    orange when it points at a leaf the robot has left, grey when it has
    forgotten the robot."""
    view, where = STATE["views"].get(leaf), STATE.get("robot_at")
    if view is None:
        status, note = "none", "no /32 (subnet route)" if leaf == "leaf5" else "no entry"
    else:
        right = (leaf == where) if view == "local" else (view == where)
        status = "ok" if right else "stale"
        note = "local" if view == "local" else (
            "/32 -> {}" if leaf == "leaf5" else "-> {}").format(view)
    VIZ.send({"action": "NODE_STATUS", "id": leaf, "status": status, "note": note})


def _refresh_leaves():
    for leaf in LEAVES:
        _show_leaf(leaf)


def _on_event(event):
    "Track each leaf's view of the robot from the monitors, and show it."
    leaf, views = event["leaf"], STATE["views"]
    if event["kind"] == "fdb" or (leaf == "leaf5" and event["kind"] == "route"):
        if not event["deleted"]:
            views[leaf] = event["where"]
        elif views.get(leaf) == event["where"]:
            views[leaf] = None
        _show_leaf(leaf)


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
    # The first link becomes the host's configured interface (IP + default route).
    for leaf in (ROBOT["from"], ROBOT["to"]):
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


def _setup_robot(tgen):
    "Bridge and bring up both leaf access ports; the robot's leaf2 link stays down."
    robot = tgen.net[ROBOT["name"]]
    # Down before leaf2's port comes up, so leaf2 never sees the robot early.
    robot.cmd_raises("ip link set {} down".format(_iface(ROBOT["to"])))
    robot.cmd_raises("ip addr flush dev {}".format(_iface(ROBOT["to"])))
    for leaf in (ROBOT["from"], ROBOT["to"]):
        robot.cmd_raises("ip link set {} address {}".format(_iface(leaf), ROBOT["mac"]))
        tgen.net[leaf].cmd_raises(
            "ip link set eth-{0} master {1}; ip link set eth-{0} up".format(
                ROBOT["name"], LEAVES[leaf]["l2br"])
        )
    robot.cmd_raises("ip link set {} up".format(_iface(ROBOT["from"])))


def setup_module(mod):
    "Sets up the pytest environment"
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()
    # Start each run with an empty evidence folder, so no trial files from
    # an earlier run with other gaps are left mixed in.
    shutil.rmtree(_evidence_dir(), ignore_errors=True)

    for leaf, spec in LEAVES.items():
        logger.info("Programming EVPN dataplane on %s", leaf)
        _setup_leaf_dataplane(tgen, leaf, spec)
    for spec in LEAVES.values():
        for host in spec["hosts"]:
            tgen.net[host].cmd_raises(
                "ip link set dev eth0 address {}".format(spec["hosts"][host][1])
            )
    _setup_robot(tgen)

    for rname, router in tgen.routers().items():
        logger.info("Loading router %s", rname)
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))
    tgen.start_router()
    _announce("Topology up; logdir={}".format(tgen.logdir))


def teardown_module(mod):
    "Teardown the pytest environment"
    for proc in STATE.get("procs", []):
        if proc.poll() is None:
            proc.kill()
    get_topogen().stop_topology()


#
# Kernel event monitors
#

IP_TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\] (.*)$")
BRIDGE_TS = re.compile(r"^Timestamp: \w+ (\w+ +\d+ \d\d:\d\d:\d\d \d{4}) (\d+) usec")
VIA = re.compile(r"via (\d+\.\d+\.\d+\.\d+)")
DST = re.compile(r" dst (\d+\.\d+\.\d+\.\d+)")
ROBOT_IP = re.compile(r"(^|\s){}(/32)?\s".format(re.escape(ROBOT["addr"])))


def _parse_fdb(line):
    "Return an event dict for an FDB line about the robot's MAC, or None."
    if ROBOT["mac"] not in line.lower():
        return None
    event = {"kind": "fdb", "deleted": line.startswith("Deleted ")}
    if "dev eth-{} ".format(ROBOT["name"]) in line + " ":
        event["where"] = "local"
    elif DST.search(line):
        event["where"] = VTEP_TO_LEAF.get(DST.search(line).group(1), "?")
    else:
        # The bridge entry on the VXLAN port; the "dst" entry says where to.
        return None
    return event


def _parse_ip(line):
    "Return an event dict for a neigh/route line about the robot's IP, or None."
    if not ROBOT_IP.search(line + " "):
        return None
    deleted = line.startswith("Deleted ")
    if " lladdr " in line or "FAILED" in line or "INCOMPLETE" in line:
        if "FAILED" in line or "INCOMPLETE" in line:
            state = "unresolved"
        else:
            state = "remote" if "extern_learn" in line else "local"
        return {"kind": "neigh", "deleted": deleted, "state": state}
    via = VIA.search(line)
    return {"kind": "route", "deleted": deleted,
            "where": VTEP_TO_LEAF.get(via.group(1), "?") if via else None}


class _Monitors:
    "Run ip/bridge monitors on every leaf and keep the robot-related events."

    def __init__(self, tgen):
        self.tgen = tgen
        self.lock = threading.Lock()
        self.events = []

    def _reader(self, leaf, kind, proc):
        path = os.path.join(_evidence_dir(), "{}_{}.log".format(leaf, kind))
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
                    line, event = m.group(2), _parse_ip(m.group(2))
                else:
                    m = BRIDGE_TS.match(line)
                    if m:
                        last_ts = time.mktime(
                            time.strptime(m.group(1), "%b %d %H:%M:%S %Y")
                        ) + int(m.group(2)) / 1e6
                        continue
                    t, event = last_ts, _parse_fdb(line) if last_ts else None
                if event:
                    event.update({"t": t, "leaf": leaf, "raw": line})
                    with self.lock:
                        self.events.append(event)
                    _on_event(event)

    def start(self):
        for leaf in LEAVES:
            cmds = [("ip", ["ip", "-ts", "monitor", "neigh", "route"])]
            if leaf in L2_LEAVES:
                # iproute2 5.15's bridge has -timestamp but not -ts.
                cmds.append(("fdb", ["bridge", "-timestamp", "monitor", "fdb"]))
            for kind, cmd in cmds:
                proc = self.tgen.net[leaf].popen(
                    ["timeout", str(RUN_SECONDS)] + cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                STATE.setdefault("procs", []).append(proc)
                threading.Thread(target=self._reader, args=(leaf, kind, proc),
                                 daemon=True).start()
        # Give the monitors time to subscribe.
        time.sleep(0.5)

    def between(self, t_from, t_to):
        with self.lock:
            return sorted((e for e in self.events if t_from <= e["t"] < t_to),
                          key=lambda e: e["t"])


class _Stream:
    """The controller's probe stream, read live from the helper's output.

    Each instruction is drawn in the visualizer once its deadline passes:
    green to the robot if delivered, red to where leaf5 sent it if missed.
    """

    def __init__(self, tgen):
        self.lock = threading.Lock()
        self.sent = {}
        self.recv = {}
        self.pending = []
        self.counts = {"delivered": 0, "missed": 0}
        self.proc = tgen.net[CONTROLLER].popen(
            ["timeout", str(RUN_SECONDS), "python3", "-u", TRAFFIC_SCRIPT, "stream",
             "--target", ROBOT["addr"], "--port", str(TRAFFIC_PORT),
             "--interval", str(PROBE_INTERVAL), "--duration", str(RUN_SECONDS - 60)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        STATE.setdefault("procs", []).append(self.proc)
        threading.Thread(target=self._reader, daemon=True).start()

    def _judge(self, now):
        while self.pending and now > self.sent[self.pending[0]] + BUDGET:
            seq = self.pending.pop(0)
            if _ok(self.recv[seq] - self.sent[seq] if seq in self.recv else None):
                self.counts["delivered"] += 1
                VIZ.send({"action": "PACKET_FLOW", "packet_type": "REACH",
                          "source": CONTROLLER, "target": ROBOT["name"],
                          "vm": ROBOT["name"]})
            else:
                self.counts["missed"] += 1
                VIZ.send({"action": "PACKET_FLOW", "packet_type": "UNREACH",
                          "source": CONTROLLER,
                          "target": STATE["views"].get("leaf5") or "leaf5",
                          "vm": ROBOT["name"]})
            VIZ.send(dict(self.counts, action="STREAM_STATS"))

    def _reader(self):
        for raw in self.proc.stdout:
            parts = _text(raw).split()
            if len(parts) != 3:
                continue
            seq = int(parts[1])
            with self.lock:
                if parts[0] == "S":
                    self.sent[seq] = float(parts[2])
                    if seq % INSTRUCTION_EVERY == 0:
                        self.pending.append(seq)
                elif parts[0] == "R":
                    self.recv.setdefault(seq, float(parts[2]))
                self._judge(time.time())

    def probes(self, t_from, t_to):
        "Return [(seq, t_send, rtt or None)] for probes sent in [t_from, t_to)."
        with self.lock:
            return [(seq, t, self.recv[seq] - t if seq in self.recv else None)
                    for seq, t in sorted(self.sent.items()) if t_from <= t < t_to]


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
    route = tgen.gears["leaf5"].vtysh_cmd(
        "show ip route vrf {} {}/32 json".format(TENANT_VRF, ROBOT["addr"]), isjson=True
    )
    if LEAVES[leaf]["lo"] not in json.dumps(route):
        return "leaf5 has no /32 for the robot via {}".format(leaf)
    return None


def _evpn_state(tgen):
    "Return each L2 leaf's EVPN MAC entry for the robot (type, VTEP, seqs, dup)."
    state = {}
    for leaf in L2_LEAVES:
        info = _robot_mac_info(tgen.gears[leaf]) or {}
        state[leaf] = {k: info.get(k) for k in (
            "type", "remoteVtep", "localSequence", "remoteSequence", "isDuplicate")}
    return state


#
# Moves
#


def _robot_cmd(tgen, cmds):
    "Run cmds in one robot shell; return the time printed by its first `date`."
    out = tgen.net[ROBOT["name"]].cmd_raises(" && ".join(["date +%s.%N"] + cmds))
    return float(out.split()[0])


def _agent(line, tag):
    "Send one command to the robot's switch agent; return the times it reports."
    agent = STATE["agent"]
    try:
        agent.stdin.write(line + "\n")
    except TypeError:
        agent.stdin.write((line + "\n").encode())
    agent.stdin.flush()
    while True:
        reply = _text(agent.stdout.readline())
        assert reply, "robot switch agent exited"
        if reply.startswith(tag + " "):
            return [float(v) for v in reply.split()[1:]]
        logger.info("robot agent: %s", reply.rstrip())


def _break():
    "The robot drops its link to leaf1; the IP waits on the (down) leaf2 link."
    return _agent("break {} {} {}".format(
        _iface(ROBOT["from"]), _iface(ROBOT["to"]), ROBOT["ip"]), "B")[0]


def _make():
    """The robot's link to leaf2 comes up and it sends one ARP for its gateway
    straight away. Return (t_make, t_arp)."""
    return tuple(_agent("make {} {} {}".format(
        _iface(ROBOT["to"]), ROBOT["ip"], ROBOT["gw"]), "M"))


def _move():
    """Break and make in one "ip -batch" call (a 0 s gap), then ARP at once.
    Return (t_break, t_make, t_arp)."""
    return tuple(_agent("move {} {} {} {}".format(
        _iface(ROBOT["from"]), _iface(ROBOT["to"]), ROBOT["ip"], ROBOT["gw"]), "V"))


def _viz_trial_summary(trial):
    "Log one trial's result and timeline in the visualizer."
    VIZ.log("   outage {}, {} of {} instructions missed; leaf2 seq {}".format(
        "{:.0f} ms".format(trial["outage_ms"]) if trial["outage_ms"] is not None
        else "NOT RECOVERED", trial["missed"], trial["instructions"],
        trial["evpn"][ROBOT["to"]]["localSequence"]), "bad" if trial["missed"] else "good")
    steps = sorted((ms, name) for name, ms in trial["milestones_ms"].items() if ms is not None)
    for ms, name in steps:
        VIZ.log("   {:+6.0f} ms  {}".format(ms, name))


def _return_home(tgen):
    "Put the robot back on leaf1 (not measured) and wait until it is settled."
    VIZ.log("Robot returns to {} for the next trial (not measured)".format(ROBOT["from"]))
    STATE["robot_at"] = None
    VIZ.link(ROBOT["to"], False)
    _refresh_leaves()
    src, dst = _iface(ROBOT["from"]), _iface(ROBOT["to"])
    _robot_cmd(tgen, [
        "ip link set {} down".format(dst),
        "ip addr flush dev {}".format(dst),
        "ip neigh flush dev {}".format(src),
        "ip addr replace {} dev {}".format(ROBOT["ip"], src),
        "ip link set {} up".format(src),
        "ip route replace default via {} dev {}".format(ROBOT["gw"], src),
    ])
    STATE["robot_at"] = ROBOT["from"]
    VIZ.link(ROBOT["from"], True)
    _refresh_leaves()
    robot = tgen.net[ROBOT["name"]]

    def _settled():
        # Re-ARP each round so leaf1 relearns the robot's IP if it lost it.
        robot.cmd("ip neigh flush dev {}; ping -c 1 -W 1 {}".format(src, ROBOT["gw"]))
        return _robot_at(tgen, ROBOT["from"])

    # Duplicate-address flags are left alone, so repeated moves show up in
    # the data if they trip FRR's limit (5 moves in 180 s by default).
    _, result = topotest.run_and_expect(_settled, None, count=30, wait=1)
    assert result is None, "Robot did not settle back on leaf1: {}".format(result)
    check_ping(CONTROLLER, ROBOT["addr"], True, count=20, wait=1)
    time.sleep(3)


def _first(events, test, after=0.0):
    for e in events:
        if e["t"] >= after and test(e):
            return e
    return None


def _milestones(events, t_break):
    "Return [(name, event or None)] for the key control-plane steps of a trial."
    src, dst = ROBOT["from"], ROBOT["to"]

    def fdb(leaf, where, deleted):
        return lambda e: (e["leaf"] == leaf and e["kind"] == "fdb"
                          and e["where"] == where and e["deleted"] == deleted)

    def route5(deleted, where=None):
        return lambda e: (e["leaf"] == "leaf5" and e["kind"] == "route"
                          and e["deleted"] == deleted
                          and (where is None or e["where"] == where))

    return [
        ("{} flushed the robot MAC (cable unplugged)".format(src),
         _first(events, fdb(src, "local", True), t_break)),
        ("leaf3 removed the robot MAC (EVPN withdraw from {})".format(src),
         _first(events, fdb("leaf3", src, True), t_break)),
        ("leaf5 withdrew the robot /32".format(),
         _first(events, route5(True), t_break)),
        ("{} learned the robot MAC (robot connected)".format(dst),
         _first(events, fdb(dst, "local", False), t_break)),
        ("{} learned the robot IP from ARP".format(dst),
         _first(events, lambda e: (e["leaf"] == dst and e["kind"] == "neigh"
                                   and e["state"] == "local" and not e["deleted"]), t_break)),
        ("leaf3 points the robot MAC at {}".format(dst),
         _first(events, fdb("leaf3", dst, False), t_break)),
        ("{} points the robot MAC at {}".format(src, dst),
         _first(events, fdb(src, dst, False), t_break)),
        ("leaf5 routes the robot /32 to {}".format(dst),
         _first(events, route5(False, dst), t_break)),
    ]


def _analyze(probes, t_break):
    "Return the outage numbers for one trial's probes."
    instr = [(seq, t, rtt) for seq, t, rtt in probes if seq % INSTRUCTION_EVERY == 0]
    result = {
        "instructions": len([i for i in instr if i[1] >= t_break]),
        "missed": sum(1 for _, t, rtt in instr if t >= t_break and not _ok(rtt)),
        "first_fail": None, "recovered_at": None,
    }
    for _, t, rtt in probes:
        if result["first_fail"] is None:
            if t >= t_break - PROBE_INTERVAL and not _ok(rtt):
                result["first_fail"] = t
        elif _ok(rtt):
            result["recovered_at"] = t
            break
    return result


def _ms(t, t0):
    return "{:+9.1f} ms".format((t - t0) * 1000)


def _write_trial(trial, events, probes):
    "Write one trial's evidence file and return its path."
    t0 = trial["t_break"]
    lines = [
        "Trial {no}: robot {src} -> {dst}, break before make, gap {gap} s".format(
            src=ROBOT["from"], dst=ROBOT["to"], **trial),
        "Robot dropped its link to {} at {} ({:.6f}); times are relative to it.".format(
            ROBOT["from"], datetime.fromtimestamp(t0).strftime("%H:%M:%S.%f"), t0),
        "",
        "== Timeline",
    ]
    steps = [(t0, "robot", "drops its link to {} (break)".format(ROBOT["from"])),
             (trial["t_make"], "robot", "link to {} up (make)".format(ROBOT["to"])),
             (trial["t_arp"], "robot", "sent ARP for its gateway")]
    for name, e in trial["milestone_events"]:
        if e:
            steps.append((e["t"], e["leaf"], name))
    if trial["first_fail"]:
        steps.append((trial["first_fail"], CONTROLLER, "first probe lost"))
    if trial["recovered_at"]:
        steps.append((trial["recovered_at"], CONTROLLER, "first probe delivered again"))
    for t, who, what in sorted(steps):
        lines.append("  {}  {:10} {}".format(_ms(t, t0), who, what))
    missing = [name for name, e in trial["milestone_events"] if not e]
    if missing:
        lines.append("  never seen in the window: " + "; ".join(missing))
    lines += [
        "",
        "== Result",
        "  outage {}, {} of {} instructions missed".format(
            "{:.1f} ms".format(trial["outage_ms"]) if trial["outage_ms"] is not None
            else "NOT RECOVERED within {:.0f} s of connecting".format(WINDOW),
            trial["missed"], trial["instructions"]),
        "  of which after the robot connected to {}: {}".format(
            ROBOT["to"], "{:.1f} ms".format(trial["after_make_ms"])
            if trial["after_make_ms"] is not None else "-"),
        "  leaf5 withdrew the /32 before the robot connected: {}".format(
            "yes" if trial["withdrawn_before_make"] else "no"),
        "",
        "== EVPN state at the end of the trial",
    ]
    for leaf, info in trial["evpn"].items():
        lines.append("  {}: {}".format(leaf, info))
    lines += ["", "== Missed instructions"]
    for seq, t, rtt in probes:
        if seq % INSTRUCTION_EVERY == 0 and t >= t0 and not _ok(rtt):
            lines.append("  {}  #{:<6} {}".format(_ms(t, t0), seq,
                                                 "LATE" if rtt is not None else "MISSED"))
    lines += ["", "== Kernel events about the robot (ip/bridge monitor)"]
    for e in events:
        lines.append("  {}  {:6} {}".format(_ms(e["t"], t0), e["leaf"], e["raw"]))
    path = os.path.join(_evidence_dir(), "trial{}_gap{}s.txt".format(trial["no"], trial["gap"]))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


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
    robot = tgen.net[ROBOT["name"]]
    _, result = topotest.run_and_expect(
        lambda: robot.cmd("ping -c 1 -W 1 {}".format(ROBOT["gw"])) and
        _robot_at(tgen, ROBOT["from"]),
        None, count=60, wait=1,
    )
    assert result is None, result
    check_ping(CONTROLLER, ROBOT["addr"], True, count=30, wait=1)
    _announce("Fabric up; robot {} on {}".format(ROBOT["name"], ROBOT["from"]))

    # Converged: leaf1 owns the robot and every other leaf points at it.
    STATE["robot_at"] = ROBOT["from"]
    STATE["views"] = {leaf: ROBOT["from"] for leaf in LEAVES}
    STATE["views"][ROBOT["from"]] = "local"
    if VIZ.start():
        _viz_publish_topology(tgen)
        _refresh_leaves()
        VIZ.log("Robot on {}. Leaves show where they think the robot is "
                "(orange = stale, grey = forgotten).".format(ROBOT["from"]))
        _announce("Publishing to visualizer at {}".format(VISUALIZER_URL))

    STATE["monitors"] = _Monitors(tgen)
    STATE["monitors"].start()
    STATE.setdefault("procs", []).append(robot.popen(
        ["timeout", str(RUN_SECONDS), "python3", TRAFFIC_SCRIPT, "echo",
         "--port", str(TRAFFIC_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
    STATE["agent"] = robot.popen(
        ["timeout", str(RUN_SECONDS), "python3", "-u", TRAFFIC_SCRIPT, "agent"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    STATE["procs"].append(STATE["agent"])
    time.sleep(0.5)


def test_baseline_no_moves():
    "With the robot standing still, no instruction is missed."
    tgen = get_topogen()
    if "monitors" not in STATE:
        pytest.skip("fabric did not converge")

    STATE["stream"] = _Stream(tgen)
    t_start = time.time()
    VIZ.log("Baseline: {:.0f} s of instructions with the robot standing still".format(BASELINE))
    time.sleep(BASELINE)
    # Skip the first second while the stream starts up.
    result = _analyze(STATE["stream"].probes(t_start + 1, time.time() - 1), t_start + 1)
    STATE["baseline"] = result
    _announce("Baseline: {} of {} instructions missed".format(
        result["missed"], result["instructions"]))
    VIZ.log("Baseline: {} of {} instructions missed".format(
        result["missed"], result["instructions"]), "good" if not result["missed"] else "bad")
    assert result["instructions"] > 0, "No probes were sent"
    assert result["missed"] == 0, (
        "Instructions were missed with the robot standing still, so the fabric "
        "itself is lossy: {}".format(result))


def test_break_before_make_moves():
    "Move the robot leaf1 -> leaf2, breaking before making, for each gap."
    tgen = get_topogen()
    if "stream" not in STATE:
        pytest.skip("baseline did not run")
    monitors, stream = STATE["monitors"], STATE["stream"]

    no = 0
    for gap in GAPS:
        for _ in range(REPEATS):
            no += 1
            VIZ.log("Trial {}: robot drops {} and stays offline {} s".format(
                no, ROBOT["from"], gap), "warn")
            STATE["robot_at"] = None
            VIZ.link(ROBOT["from"], False)
            _refresh_leaves()
            if gap > 0:
                t_break = _break()
                time.sleep(max(0.0, t_break + gap - time.time()))
            STATE["robot_at"] = ROBOT["to"]
            VIZ.link(ROBOT["to"], True)
            _refresh_leaves()
            VIZ.log("   robot connects to {}".format(ROBOT["to"]), "warn")
            if gap > 0:
                t_make, t_arp = _make()
            else:
                t_break, t_make, t_arp = _move()
            time.sleep(max(0.0, t_make + WINDOW - time.time()))
            t_end = time.time()
            evpn = _evpn_state(tgen)

            events = monitors.between(t_break - 0.05, t_end)
            # Leave out probes still in flight when the window closed.
            probes = stream.probes(t_break - 0.5, t_end - BUDGET - 0.05)
            trial = {"no": no, "gap": gap, "t_break": t_break, "t_make": t_make,
                     "t_arp": t_arp, "gap_actual_ms": (t_make - t_break) * 1000,
                     "evpn": evpn}
            trial.update(_analyze(probes, t_break))
            trial["milestone_events"] = _milestones(events, t_break)
            trial["milestones_ms"] = {
                name: (e["t"] - t_break) * 1000 if e else None
                for name, e in trial["milestone_events"]}
            withdrawn = trial["milestone_events"][2][1]
            trial["withdrawn_before_make"] = bool(withdrawn and withdrawn["t"] < t_make)
            if trial["recovered_at"] and trial["first_fail"]:
                trial["outage_ms"] = (trial["recovered_at"] - trial["first_fail"]) * 1000
                trial["after_make_ms"] = max(0.0, (trial["recovered_at"] - t_make) * 1000)
            else:
                trial["outage_ms"] = trial["after_make_ms"] = None
            trial["file"] = _write_trial(trial, events, probes)
            del trial["milestone_events"]
            STATE["trials"].append(trial)

            _announce("Trial {}: gap {} s (actual {:.0f} ms): outage {}, {} of {} "
                      "instructions missed; leaf2 seq {}; duplicate flagged: {}".format(
                          no, gap, trial["gap_actual_ms"],
                          "{:.0f} ms".format(trial["outage_ms"])
                          if trial["outage_ms"] is not None else "NOT RECOVERED",
                          trial["missed"], trial["instructions"],
                          evpn[ROBOT["to"]]["localSequence"],
                          [l for l, i in evpn.items() if i["isDuplicate"]] or "no"))
            _viz_trial_summary(trial)
            _return_home(tgen)

    with open(os.path.join(_evidence_dir(), "results.json"), "w") as f:
        json.dump({"settings": {"gaps_s": GAPS, "repeats": REPEATS, "window_s": WINDOW,
                                "budget_ms": BUDGET * 1000},
                   "baseline": STATE.get("baseline"), "trials": STATE["trials"]},
                  f, indent=2, default=str)
    _announce("Evidence written to {}".format(_evidence_dir()))
    assert STATE["trials"] and all(t["instructions"] for t in STATE["trials"]), (
        "No instructions were sent during the trials")


def test_break_before_make_summary():
    "Print one row per trial."
    if not STATE["trials"]:
        pytest.skip("no trials ran")

    def ms(value):
        return "{:.0f}".format(value) if value is not None else "-"

    names = {
        "withdraw": "leaf5 withdrew the robot /32",
        "mac": "{} learned the robot MAC (robot connected)".format(ROBOT["to"]),
        "ip": "{} learned the robot IP from ARP".format(ROBOT["to"]),
        "route": "leaf5 routes the robot /32 to {}".format(ROBOT["to"]),
    }
    lines = ["  gap_s  gap_ms  /32_gone  leaf2_mac  leaf2_ip  /32_back  outage_ms  "
             "after_make_ms  missed  leaf2_seq  dup"]
    for t in STATE["trials"]:
        m = t["milestones_ms"]
        lines.append("  {:>5}  {:>6}  {:>8}  {:>9}  {:>8}  {:>8}  {:>9}  {:>13}  {:>6}  {:>9}  {}".format(
            t["gap"], ms(t["gap_actual_ms"]), ms(m[names["withdraw"]]), ms(m[names["mac"]]),
            ms(m[names["ip"]]), ms(m[names["route"]]),
            ms(t["outage_ms"]) if t["outage_ms"] is not None else "LOST",
            ms(t["after_make_ms"]), "{}/{}".format(t["missed"], t["instructions"]),
            t["evpn"][ROBOT["to"]]["localSequence"],
            ",".join(l for l, i in t["evpn"].items() if i["isDuplicate"]) or "no"))
    table = "Break-before-make trials (times in ms after the robot dropped leaf1)\n{}".format(
        "\n".join(lines))
    _announce(table)

    # How consistent is each gap: min / median / max over its trials.
    stats = ["", "Consistency per gap (min / median / max over trials)"]
    columns = [
        ("actual gap ms", lambda t: t["gap_actual_ms"]),
        ("/32 gone ms", lambda t: t["milestones_ms"][names["withdraw"]]),
        ("leaf2 MAC ms", lambda t: t["milestones_ms"][names["mac"]]),
        ("leaf2 IP ms", lambda t: t["milestones_ms"][names["ip"]]),
        ("/32 back ms", lambda t: t["milestones_ms"][names["route"]]),
        ("outage ms", lambda t: t["outage_ms"]),
        ("after make ms", lambda t: t["after_make_ms"]),
        ("missed instr", lambda t: t["missed"]),
    ]
    for gap in sorted(set(t["gap"] for t in STATE["trials"])):
        trials = [t for t in STATE["trials"] if t["gap"] == gap]
        stats.append("  gap {} s: {} trials, {} recovered; leaf2 sequence {}; "
                     "duplicate flagged in {}".format(
                         gap, len(trials),
                         sum(1 for t in trials if t["outage_ms"] is not None),
                         ", ".join("{}: {}x".format(seq, n) for seq, n in sorted(
                             _count(t["evpn"][ROBOT["to"]]["localSequence"]
                                    for t in trials).items(), key=lambda kv: str(kv[0]))),
                         sum(1 for t in trials
                             if any(i["isDuplicate"] for i in t["evpn"].values()))))
        for label, get in columns:
            values = sorted(v for v in (get(t) for t in trials) if v is not None)
            if values:
                stats.append("    {:14} {:>7} / {:>7} / {:>7}".format(
                    label, ms(values[0]), ms(statistics.median(values)), ms(values[-1])))
            else:
                stats.append("    {:14} never seen".format(label))
    _announce("\n".join(stats))
    with open(os.path.join(_evidence_dir(), "summary.txt"), "w") as f:
        f.write(table + "\n" + "\n".join(stats) + "\n")
    VIZ.log("Summary of {} trials written to summary.txt".format(len(STATE["trials"])), "warn")
    for line in stats[1:]:
        VIZ.log(line)


def _count(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return counts


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
