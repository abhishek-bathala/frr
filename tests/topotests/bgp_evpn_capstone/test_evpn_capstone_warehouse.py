#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_warehouse.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
test_evpn_capstone_warehouse.py: Check the Capstone fabric against the
warehouse robot requirements.

Requirements (from the project brief):

  * Robots exchange instructions with the controller every 100 ms.
  * Robots move about five times per minute.

The fabric is the original Capstone topology from test_evpn_capstone.py
(project_topology.pdf). host11 plays the robot and the controller behind leaf5
plays the warehouse controller, so instructions are routed across the L3VNI
(VRF500), the same path a real robot's traffic would take.

Each leaf access port stands in for an access point (AP). host11 is cabled to
two of them, and both stay bridged and up for the whole run, the way real APs
stay up when a robot drives away:

  * host11:eth0      <-> leaf1:eth-host11   ("AP" on leaf1, robot starts here)
  * host11:eth-leaf2 <-> leaf2:eth-host11   ("AP" on leaf2)

A move ("roam") turns the robot's radio off on one link and on at the other.
The radio is a tc drop filter on host11's side, so the old leaf never sees its
port go down and keeps the robot's MAC until EVPN tells it the robot has
moved. That is the RFC 7432 section 15 MAC-mobility case: the new leaf learns
the MAC while the old leaf still advertises it, and advertises a higher
sequence number. After the switch, the robot sends one ping to its gateway to
announce itself, as a roaming client sends a gratuitous ARP.

Measurements:

  * Data plane: warehouse_traffic.py sends a numbered UDP probe from the
    controller to the robot every 10 ms, and the robot echoes it back. Every
    10th probe counts as an instruction. An instruction is missed if its round
    trip is lost or takes longer than 100 ms. The outage after a move is the
    time from the first failed probe to the next successful one, accurate to
    one probe interval.

  * Control plane (for diagnosis): during each move, one thread per leaf polls
    vtysh and records when that leaf points at the robot's new location, as in
    test_evpn_capstone_convergence.py. These times are accurate to about
    +/-100 ms because each vtysh call takes about that long.

  * Mobility state: the MAC-mobility sequence number and the duplicate-address
    detection (DAD) flag on each leaf after every move. FRR's default DAD
    limit is 5 moves in 180 s, so a robot moving 5 times per minute is
    expected to trip it.

Tests:

  1. test_fabric_converged: the fabric is up and the robot is on leaf1.
  2. test_baseline_instruction_stream: the instruction stream with no moves.
  3. test_sustained_mobility: the robot roams leaf1 <-> leaf2 at the target
     move rate while the stream runs.
  4. test_requirements_verdict: prints a requirement-by-requirement table.
     By default it reports only; set WAREHOUSE_ENFORCE=true to fail the test
     when a requirement is not met.

Results are printed (run pytest with -s) and written as JSON to
warehouse_results.json in the topotest log directory.

Environment variables:
  WAREHOUSE_MOVES_PER_MINUTE     target move rate (default 5)
  WAREHOUSE_MOVES                number of moves in the sustained run (default 10)
  WAREHOUSE_INSTRUCTION_PERIOD   seconds between instructions (default 0.1)
  WAREHOUSE_DEADLINE             max instruction round trip, seconds (default 0.1)
  WAREHOUSE_PROBE_INTERVAL       seconds between probes (default 0.01)
  WAREHOUSE_BASELINE_SECONDS     length of the no-move baseline (default 20)
  WAREHOUSE_ROAM_GAP             seconds the radio is off between APs (default 0)
  WAREHOUSE_REARP_AFTER          if > 0, the robot ARPs for its gateway again
                                 this many seconds after each move (default 0)
  WAREHOUSE_MAX_MISSED           missed instructions allowed per move (default 0)
  WAREHOUSE_WATCH_CONTROL_PLANE  "false" to skip the per-leaf vtysh polling,
                                 which adds load while timing (default "true")
  WAREHOUSE_ENFORCE              "true" to fail on unmet requirements
                                 (default "false")
  CONVERGENCE_POLL_INTERVAL      seconds between vtysh checks per leaf (default 0.05)
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

# Save the Current Working Directory to find configuration files.
CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
# Import topogen and topotest helpers
from lib import topotest
from lib.checkping import check_ping
from lib.topogen import Topogen, get_topogen
from lib.topolog import logger

pytestmark = [pytest.mark.bgpd, pytest.mark.evpn]

# Shared anycast gateway MAC used by every SVI.
ANYCAST_MAC = "00:00:5e:00:01:01"

SPINES = {
    "spine1": "10.0.0.101",
    "spine2": "10.0.0.102",
}

# Per-leaf description, identical to test_evpn_capstone.py.
# "hosts" maps host name -> (ip/prefix, mac).
LEAVES = {
    "leaf1": {
        "lo": "10.0.0.1",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {
            "host11": ("45.0.0.11/24", "00:00:00:00:00:11"),
            "host12": ("45.0.0.12/24", "00:00:00:00:00:12"),
        },
        "gw": "45.0.0.1",
    },
    "leaf2": {
        "lo": "10.0.0.2",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {
            "host21": ("45.0.0.21/24", "00:00:00:00:00:21"),
            "host22": ("45.0.0.22/24", "00:00:00:00:00:22"),
        },
        "gw": "45.0.0.1",
    },
    "leaf3": {
        "lo": "10.0.0.3",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {
            "host31": ("45.0.0.31/24", "00:00:00:00:00:31"),
            "host32": ("45.0.0.32/24", "00:00:00:00:00:32"),
        },
        "gw": "45.0.0.1",
    },
    "leaf4": {
        "lo": "10.0.0.4",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {
            "host41": ("45.0.0.41/24", "00:00:00:00:00:41"),
            "host42": ("45.0.0.42/24", "00:00:00:00:00:42"),
        },
        "gw": "45.0.0.1",
    },
    "leaf5": {
        "lo": "10.0.0.5",
        "l2vni": 2000,
        "l2br": "br2000",
        "svi": "20.0.0.1/24",
        "hosts": {
            "controller": ("20.0.0.51/24", "00:00:00:00:00:51"),
        },
        "gw": "20.0.0.1",
    },
}

L3VNI = 500
L3BR = "br500"
TENANT_VRF = "VRF500"

# The robot. "ifaces" maps leaf -> host11 interface cabled to that leaf; the
# leaf side of either link is always eth-host11.
ROBOT = {
    "name": "host11",
    "ip": "45.0.0.11/24",
    "addr": "45.0.0.11",
    "mac": "00:00:00:00:00:11",
    "gw": "45.0.0.1",
    "l2vni": 1000,
    "home_leaf": "leaf1",
    "other_leaf": "leaf2",
    "ifaces": {"leaf1": "eth0", "leaf2": "eth-leaf2"},
}
CONTROLLER = "controller"
CONTROLLER_ADDR = "20.0.0.51"

TRAFFIC_SCRIPT = os.path.join(CWD, "warehouse_traffic.py")
TRAFFIC_PORT = 5005


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _env_bool(name, default):
    return os.environ.get(name, default).lower() == "true"


MOVES_PER_MINUTE = _env_float("WAREHOUSE_MOVES_PER_MINUTE", "5")
MOVES = int(os.environ.get("WAREHOUSE_MOVES", "10"))
INSTRUCTION_PERIOD = _env_float("WAREHOUSE_INSTRUCTION_PERIOD", "0.1")
DEADLINE = _env_float("WAREHOUSE_DEADLINE", "0.1")
PROBE_INTERVAL = min(
    INSTRUCTION_PERIOD, _env_float("WAREHOUSE_PROBE_INTERVAL", "0.01")
)
BASELINE_SECONDS = _env_float("WAREHOUSE_BASELINE_SECONDS", "20")
ROAM_GAP = _env_float("WAREHOUSE_ROAM_GAP", "0")
REARP_AFTER = _env_float("WAREHOUSE_REARP_AFTER", "0")
MAX_MISSED = int(os.environ.get("WAREHOUSE_MAX_MISSED", "0"))
WATCH_CONTROL_PLANE = _env_bool("WAREHOUSE_WATCH_CONTROL_PLANE", "true")
ENFORCE = _env_bool("WAREHOUSE_ENFORCE", "false")
POLL_INTERVAL = max(
    0.01, _env_float("CONVERGENCE_POLL_INTERVAL", "0.05")
)

MOVE_PERIOD = 60.0 / MOVES_PER_MINUTE
# Quiet time before the first move, so the stream has a clean start.
PRE_ROLL = 3.0

# tc filter priority used for the robot's "radio off" drop filters.
RADIO_PREF = 10

RESULTS = {"config": {}, "baseline": None, "moves": [], "verdict": []}


def _announce(msg):
    "Print a status line to the pytest console (needs pytest -s)."
    print("[warehouse] {}".format(msg), flush=True)
    logger.info(msg)


def _save_results(tgen):
    path = os.path.join(tgen.logdir, "warehouse_results.json")
    with open(path, "w") as f:
        json.dump(RESULTS, f, indent=2)
    return path


def build_topo(tgen):
    "Build the Capstone EVPN fabric plus a second access link for the robot."

    for spine in SPINES:
        tgen.add_router(spine)
    for leaf in LEAVES:
        tgen.add_router(leaf)

    # Full spine-leaf mesh: every leaf to every spine.
    # Use direct point-to-point links so the auto-generated switch interface
    # names do not exceed the 15-character Linux interface name limit.
    for spine in SPINES:
        for leaf in LEAVES:
            tgen.add_link(
                tgen.gears[spine],
                tgen.gears[leaf],
                "eth-{}".format(leaf),
                "eth-{}".format(spine),
            )

    # Hosts hang off their leaf access ports.
    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            ip = spec["hosts"][host][0]
            tgen.add_host(host, ip, "via {}".format(spec["gw"]))
            tgen.add_link(
                tgen.gears[host],
                tgen.gears[leaf],
                "eth0",
                "eth-{}".format(host),
            )

    # Second access link for the robot. _setup_robot_links() keeps its IP on
    # eth0, since topogen would otherwise configure this interface instead.
    other = ROBOT["other_leaf"]
    tgen.add_link(
        tgen.gears[ROBOT["name"]],
        tgen.gears[other],
        ROBOT["ifaces"][other],
        "eth-{}".format(ROBOT["name"]),
    )


def _setup_leaf_dataplane(tgen, leaf, spec):
    "Create VRF, L3VNI, L2VNI, SVI and enslave access ports on a leaf."
    node = tgen.net[leaf]
    lo = spec["lo"]
    l2vni = spec["l2vni"]
    l2br = spec["l2br"]

    node.cmd_raises(
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
""".format(
            vrf=TENANT_VRF,
            l3vni=L3VNI,
            l3br=L3BR,
            l2vni=l2vni,
            l2br=l2br,
            lo=lo,
            svi=spec["svi"],
            mac=ANYCAST_MAC,
        )
    )

    for host in spec["hosts"]:
        node.cmd_raises(
            "ip link set eth-{host} master {l2br}; "
            "ip link set eth-{host} up".format(host=host, l2br=l2br)
        )


def _radio_cmd(iface, on):
    "Return the shell command that turns the robot's radio on iface on or off."
    if on:
        return (
            "tc filter del dev {i} ingress pref {p} && "
            "tc filter del dev {i} egress pref {p}"
        ).format(i=iface, p=RADIO_PREF)
    return (
        "tc filter add dev {i} ingress pref {p} matchall action drop && "
        "tc filter add dev {i} egress pref {p} matchall action drop"
    ).format(i=iface, p=RADIO_PREF)


def _setup_robot_links(tgen):
    "Bridge both robot links; radio on toward leaf1 only, IP on eth0."
    name = ROBOT["name"]
    home_if = ROBOT["ifaces"][ROBOT["home_leaf"]]
    other_if = ROBOT["ifaces"][ROBOT["other_leaf"]]
    host = tgen.net[name]

    # Radio off on the second link before anything is sent on it.
    host.cmd_raises("ip link set {} address {}".format(other_if, ROBOT["mac"]))
    for iface in (home_if, other_if):
        host.cmd_raises("tc qdisc add dev {} clsact".format(iface))
    host.cmd_raises(_radio_cmd(other_if, False))

    # topogen puts a host's IP and default route on its alphabetically first
    # interface, and "eth-leaf2" sorts before "eth0". Move them to eth0.
    host.cmd_raises("ip addr flush dev {}".format(other_if))
    host.cmd_raises("ip addr replace {} dev {}".format(ROBOT["ip"], home_if))
    host.cmd_raises("ip route replace default via {}".format(ROBOT["gw"]))
    host.cmd_raises("ip link set {} up".format(other_if))

    # The leaf2 "AP" port is always up and bridged.
    other = tgen.net[ROBOT["other_leaf"]]
    other.cmd_raises(
        "ip link set eth-{} master {}".format(name, LEAVES[ROBOT["other_leaf"]]["l2br"])
    )
    other.cmd_raises("ip link set eth-{} up".format(name))


def setup_module(mod):
    "Sets up the pytest environment"
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    # Program the VXLAN/bridge/SVI dataplane on every leaf before FRR starts.
    for leaf, spec in LEAVES.items():
        logger.info("Programming EVPN dataplane on %s", leaf)
        _setup_leaf_dataplane(tgen, leaf, spec)

    # Fix host MAC addresses to match the diagram.
    for spec in LEAVES.values():
        for host in spec["hosts"]:
            mac = spec["hosts"][host][1]
            tgen.net[host].cmd_raises(
                "ip link set dev eth0 address {}".format(mac)
            )

    _setup_robot_links(tgen)

    for rname, router in tgen.routers().items():
        logger.info("Loading router %s", rname)
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))

    tgen.start_router()


def teardown_module(mod):
    "Teardown the pytest environment"
    tgen = get_topogen()
    tgen.stop_topology()


def _peers_established(router, afi, expected_peers):
    "Return None when all expected peers are Established, else a message."
    output = router.vtysh_cmd("show bgp {} summary json".format(afi), isjson=True)
    peers = output.get("peers", {})
    for peer in expected_peers:
        state = peers.get(peer, {}).get("state")
        if state != "Established":
            return "{} {} peer {} state {}".format(router.name, afi, peer, state)
    return None


def _expected_peers():
    "Return dict of router -> list of expected unnumbered peer interface names."
    expected = {}
    for spine in SPINES:
        expected[spine] = ["eth-{}".format(leaf) for leaf in LEAVES]
    for leaf in LEAVES:
        expected[leaf] = ["eth-{}".format(spine) for spine in SPINES]
    return expected


def _wait_for_evpn_fabric(tgen):
    "Block until underlay, EVPN sessions, and VNIs are present."
    for rname, peers in _expected_peers().items():
        router = tgen.gears[rname]
        for afi in ("ipv4 unicast", "l2vpn evpn"):
            _, result = topotest.run_and_expect(
                lambda r=router, a=afi, p=peers: _peers_established(r, a, p),
                None,
                count=90,
                wait=1,
            )
            assert result is None, "Fabric did not converge: {}".format(result)

    for leaf, spec in LEAVES.items():
        router = tgen.gears[leaf]
        wanted = {str(L3VNI), str(spec["l2vni"])}

        def _check(router=router, wanted=wanted):
            output = router.vtysh_cmd("show evpn vni json", isjson=True)
            missing = wanted - set(output.keys())
            if missing:
                return "{} missing VNIs {}".format(router.name, sorted(missing))
            return None

        _, result = topotest.run_and_expect(_check, None, count=60, wait=1)
        assert result is None, result


def _prime_host_adjacencies(tgen):
    "Generate host traffic so every leaf learns its local host MAC/IP."
    for spec in LEAVES.values():
        for host in spec["hosts"]:
            tgen.net[host].cmd("ping -c 2 -W 1 {}".format(spec["gw"]))


def _robot_mac_info(router):
    "Return the EVPN MAC JSON object for the robot, or None if missing."
    output = router.vtysh_cmd(
        "show evpn mac vni {} mac {} json".format(ROBOT["l2vni"], ROBOT["mac"]),
        isjson=True,
    )
    if not output:
        return None
    info = output.get(ROBOT["mac"], output)
    if not isinstance(info, dict) or "type" not in info:
        return None
    return info


# The location checks below ignore the duplicate flag on purpose: the robot is
# expected to trip duplicate detection, and that is reported separately.


def _mac_is_local(router):
    "Return None if the robot's MAC is local on router."
    info = _robot_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for {}".format(router.name, ROBOT["mac"])
    if info.get("type") != "local":
        return "{} robot MAC type {} (want local)".format(
            router.name, info.get("type")
        )
    return None


def _mac_is_remote(router, vtep):
    "Return None if the robot's MAC is remote via vtep."
    info = _robot_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for {}".format(router.name, ROBOT["mac"])
    if info.get("type") != "remote" or info.get("remoteVtep") != vtep:
        return "{} robot MAC {} via {} (want remote via {})".format(
            router.name, info.get("type"), info.get("remoteVtep"), vtep
        )
    return None


def _vrf_host_via_vtep(router, prefix, vtep):
    "Return None if VRF500 has a selected BGP route for prefix via vtep."
    output = router.vtysh_cmd(
        "show ip route vrf {} {} json".format(TENANT_VRF, prefix), isjson=True
    )
    for entry in output.get(prefix, []):
        if entry.get("protocol") != "bgp" or not entry.get("selected"):
            continue
        for nh in entry.get("nexthops", []):
            if nh.get("ip") == vtep and nh.get("active", True):
                return None
    return "{} has no BGP {} via {} in {}".format(
        router.name, prefix, vtep, TENANT_VRF
    )


def _location_checks(tgen, leaf):
    """Return {leaf: (description, check)} for "the robot is on leaf".

    Each check returns None once that leaf's entry for the robot points at
    the given leaf.
    """
    vtep = LEAVES[leaf]["lo"]
    prefix = "{}/32".format(ROBOT["addr"])
    checks = {}
    for name, spec in LEAVES.items():
        router = tgen.gears[name]
        if name == leaf:
            checks[name] = ("MAC local", lambda r=router: _mac_is_local(r))
        elif spec["l2vni"] == ROBOT["l2vni"]:
            checks[name] = (
                "MAC remote via {}".format(vtep),
                lambda r=router: _mac_is_remote(r, vtep),
            )
        else:
            checks[name] = (
                "{} via {}".format(prefix, vtep),
                lambda r=router: _vrf_host_via_vtep(r, prefix, vtep),
            )
    return checks


def _wait_for_robot_at(tgen, leaf):
    "Assert (with retries) that every leaf agrees the robot is on leaf."
    for name, (desc, check) in _location_checks(tgen, leaf).items():
        _, result = topotest.run_and_expect(check, None, count=90, wait=1)
        assert result is None, "{} ({}): {}".format(name, desc, result)


def _mobility_state(tgen):
    "Return {leaf: {type, seq, duplicate, detections}} for VNI 1000 leaves."
    state = {}
    for name, spec in LEAVES.items():
        if spec["l2vni"] != ROBOT["l2vni"]:
            continue
        info = _robot_mac_info(tgen.gears[name]) or {}
        state[name] = {
            "type": info.get("type"),
            "localSequence": int(info.get("localSequence", 0)),
            "remoteSequence": int(info.get("remoteSequence", 0)),
            "duplicate": bool(info.get("isDuplicate", False)),
            "detectionCount": int(info.get("detectionCount", 0)),
        }
    return state


def _snapshot(tgen):
    """Return the robot's forwarding state on every leaf, as text.

    Taken at the end of each move window, so a move that never recovered can
    be traced to the leaf and table that still points the wrong way.
    """
    snap = {}
    for name, spec in LEAVES.items():
        router = tgen.gears[name]
        node = tgen.net[name]
        entry = {
            "kernel_neigh": node.cmd("ip neigh show {}".format(ROBOT["addr"])).strip(),
            "vrf_route": router.vtysh_cmd(
                "show ip route vrf {} {}".format(TENANT_VRF, ROBOT["addr"])
            ).strip(),
        }
        if spec["l2vni"] == ROBOT["l2vni"]:
            entry["evpn_mac"] = router.vtysh_cmd(
                "show evpn mac vni {} mac {}".format(ROBOT["l2vni"], ROBOT["mac"])
            ).strip()
            entry["evpn_arp"] = router.vtysh_cmd(
                "show evpn arp-cache vni {} ip {}".format(
                    ROBOT["l2vni"], ROBOT["addr"])
            ).strip()
            entry["bridge_fdb"] = node.cmd(
                "bridge fdb show | grep -i {}".format(ROBOT["mac"])
            ).strip()
        snap[name] = entry
    return snap


class _ControlPlaneWatcher:
    """Poll each leaf in its own thread and timestamp when its check passes.

    Times are time.monotonic() values. For each leaf, "t_done" is when the
    first passing check finished, and "resolution" is the time since the
    previous failing check started (the real change happened in between).
    """

    def __init__(self, checks, timeout):
        self.checks = checks
        self.timeout = timeout
        self.stop = threading.Event()
        self.state = {
            name: {"done": threading.Event(), "t_done": None,
                   "resolution": None, "last": None}
            for name in checks
        }
        self.threads = []

    def _run(self, name, check, deadline):
        st = self.state[name]
        prev_start = time.monotonic()
        while not self.stop.is_set() and time.monotonic() < deadline:
            start = time.monotonic()
            try:
                result = check()
            except Exception as error:  # pylint: disable=broad-except
                # vtysh may return partial JSON while state is changing.
                result = "check error: {}".format(error)
            end = time.monotonic()
            if result is None:
                st["t_done"] = end
                st["resolution"] = end - prev_start
                st["done"].set()
                return
            st["last"] = result
            prev_start = start
            self.stop.wait(POLL_INTERVAL)

    def precheck(self):
        "Assert no check passes yet. Run this before the move."
        for name, (desc, check) in self.checks.items():
            assert check() is not None, (
                "{} already satisfies '{}' before the move".format(name, desc)
            )

    def start(self):
        "Start one polling thread per leaf. Run this right before the move."
        deadline = time.monotonic() + self.timeout
        for name, (_, check) in self.checks.items():
            thread = threading.Thread(
                target=self._run, args=(name, check, deadline), daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def finish(self, t_ref):
        """Wait for every thread, then return {leaf: ms or None} since t_ref.

        None means the leaf did not reach the new location within the timeout.
        """
        for thread in self.threads:
            thread.join(self.timeout + 5)
        self.stop.set()
        return {
            name: (
                round((st["t_done"] - t_ref) * 1000.0, 1)
                if st["done"].is_set() else None
            )
            for name, st in self.state.items()
        }


def _roam(tgen, old_leaf, new_leaf):
    "Move the robot's radio and IP from old_leaf's AP to new_leaf's AP."
    host = tgen.net[ROBOT["name"]]
    old_if = ROBOT["ifaces"][old_leaf]
    new_if = ROBOT["ifaces"][new_leaf]

    host.cmd_raises(
        _radio_cmd(old_if, False) + " && ip addr flush dev {}".format(old_if)
    )
    if ROAM_GAP > 0:
        time.sleep(ROAM_GAP)
    host.cmd_raises(
        _radio_cmd(new_if, True)
        + " && ip addr replace {} dev {}".format(ROBOT["ip"], new_if)
        + " && ip route replace default via {} dev {}".format(ROBOT["gw"], new_if)
    )
    # Announce the robot on the new AP, like a roaming client's gratuitous
    # ARP. The new leaf learns the MAC from this.
    host.cmd("ping -c 1 -W 1 {}".format(ROBOT["gw"]))

    if REARP_AFTER > 0:
        # Flush the gateway's ARP entry so the next ping sends a new ARP.
        timer = threading.Timer(
            REARP_AFTER,
            host.cmd,
            ["ip neigh flush dev {i}; ping -c 1 -W 1 {gw}".format(
                i=new_if, gw=ROBOT["gw"])],
        )
        timer.daemon = True
        timer.start()


def _start_echo(tgen, duration):
    "Start the robot-side echo responder for duration seconds."
    proc = tgen.net[ROBOT["name"]].popen(
        ["python3", TRAFFIC_SCRIPT, "echo", "--port", str(TRAFFIC_PORT),
         "--duration", str(duration)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Let the responder bind before the first probe.
    time.sleep(0.5)
    return proc


def _start_stream(tgen, duration):
    "Start the controller-side instruction stream for duration seconds."
    return tgen.net[CONTROLLER].popen(
        ["python3", TRAFFIC_SCRIPT, "send", "--target", ROBOT["addr"],
         "--port", str(TRAFFIC_PORT), "--interval", str(PROBE_INTERVAL),
         "--duration", str(duration)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _collect_stream(proc, duration):
    "Wait for the stream to finish and return its parsed JSON log."
    out, err = proc.communicate(timeout=duration + 30)
    if isinstance(out, bytes):
        out = out.decode()
        err = err.decode()
    assert proc.returncode == 0, "Instruction stream failed: {}".format(err)
    return json.loads(out)


def _stop(proc):
    "Best-effort stop of a helper process that has not exited yet."
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _analyze(stream, t_from, t_to):
    """Summarize the probes sent in [t_from, t_to).

    A probe is good if its echo came back within DEADLINE. Every
    INSTRUCTION_PERIOD / PROBE_INTERVAL-th probe counts as an instruction.
    """
    interval = stream["interval"]
    recv = {int(seq): t for seq, t in stream["recv"]}
    every = max(1, int(round(INSTRUCTION_PERIOD / interval)))

    probes = lost = late = 0
    rtts = []
    instructions = missed = run = max_run = 0
    outage_start = None
    outage_ms = 0.0
    last_send = None

    for seq, t_send in enumerate(stream["sent"]):
        if t_send < t_from or t_send >= t_to:
            continue
        probes += 1
        last_send = t_send
        t_recv = recv.get(seq)
        if t_recv is None:
            lost += 1
            good = False
        else:
            rtts.append((t_recv - t_send) * 1000.0)
            good = t_recv - t_send <= DEADLINE
            if not good:
                late += 1

        # Outage: from the first failed probe to the next good one.
        if good and outage_start is not None:
            outage_ms = max(outage_ms, (t_send - outage_start) * 1000.0)
            outage_start = None
        elif not good and outage_start is None:
            outage_start = t_send

        if seq % every == 0:
            instructions += 1
            if good:
                run = 0
            else:
                missed += 1
                run += 1
                max_run = max(max_run, run)

    # Still out at the end of the window: count up to the last probe sent.
    unrecovered = outage_start is not None
    if unrecovered:
        outage_ms = max(outage_ms, (last_send + interval - outage_start) * 1000.0)

    return {
        "probes": probes,
        "lost": lost,
        "late": late,
        "instructions": instructions,
        "missed_instructions": missed,
        "max_consecutive_missed": max_run,
        "outage_ms": round(outage_ms, 1),
        "unrecovered": unrecovered,
        "rtt_ms": {
            "p50": _percentile(rtts, 50),
            "p99": _percentile(rtts, 99),
            "max": round(max(rtts), 2) if rtts else None,
        },
    }


def _dad_config(tgen):
    "Return the duplicate-address-detection settings zebra is using."
    output = tgen.gears[ROBOT["home_leaf"]].vtysh_cmd("show evpn json", isjson=True)
    return {
        "enabled": output.get("isDuplicateAddrDetection"),
        "maxMoves": output.get("maxMoves"),
        "detectionTime": output.get("detectionTime"),
        "freeze": output.get("isDetectionFreeze"),
    }


def test_fabric_converged():
    "Bring the fabric up and confirm every leaf sees the robot on leaf1."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    _wait_for_evpn_fabric(tgen)
    _prime_host_adjacencies(tgen)
    _wait_for_robot_at(tgen, ROBOT["home_leaf"])
    check_ping(CONTROLLER, ROBOT["addr"], True, count=30, wait=1)
    check_ping(ROBOT["name"], CONTROLLER_ADDR, True, count=30, wait=1)

    RESULTS["config"] = {
        "moves_per_minute": MOVES_PER_MINUTE,
        "move_period_s": MOVE_PERIOD,
        "moves": MOVES,
        "instruction_period_s": INSTRUCTION_PERIOD,
        "deadline_s": DEADLINE,
        "probe_interval_s": PROBE_INTERVAL,
        "roam_gap_s": ROAM_GAP,
        "rearp_after_s": REARP_AFTER,
        "max_missed_per_move": MAX_MISSED,
        "control_plane_watch": WATCH_CONTROL_PLANE,
        "poll_interval_s": POLL_INTERVAL,
        "dad": _dad_config(tgen),
    }
    _announce("Fabric up; robot {} on {}; DAD settings {}".format(
        ROBOT["name"], ROBOT["home_leaf"], RESULTS["config"]["dad"]
    ))


def test_baseline_instruction_stream():
    "Run the instruction stream with the robot standing still."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    echo = _start_echo(tgen, BASELINE_SECONDS + 30)
    try:
        stream = _collect_stream(
            _start_stream(tgen, BASELINE_SECONDS), BASELINE_SECONDS
        )
    finally:
        _stop(echo)

    # Skip the first second so ARP and socket setup do not count.
    t_start = stream["sent"][0] + 1.0
    baseline = _analyze(stream, t_start, float("inf"))
    RESULTS["baseline"] = baseline
    _save_results(tgen)

    _announce(
        "Baseline ({:.0f} s, no moves): {} instructions, {} missed; "
        "probe RTT p50 {} ms, p99 {} ms, max {} ms; {} lost, {} late".format(
            BASELINE_SECONDS, baseline["instructions"],
            baseline["missed_instructions"], baseline["rtt_ms"]["p50"],
            baseline["rtt_ms"]["p99"], baseline["rtt_ms"]["max"],
            baseline["lost"], baseline["late"],
        )
    )
    assert baseline["probes"] > 0, "No probes were sent"
    assert baseline["rtt_ms"]["p50"] is not None, (
        "No instruction reached the robot and back; the stream is broken"
    )


def test_sustained_mobility():
    "Roam the robot leaf1 <-> leaf2 at the target rate with the stream running."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    # Leave time before each move for the pre-move checks, which run vtysh.
    watch_timeout = min(30.0, MOVE_PERIOD - 2.0)
    watch = WATCH_CONTROL_PLANE and watch_timeout >= 2.0
    if WATCH_CONTROL_PLANE and not watch:
        _announce("Move period {:.1f} s is too short to watch the control "
                  "plane; timing the data plane only".format(MOVE_PERIOD))

    duration = PRE_ROLL + MOVES * MOVE_PERIOD + 1.0
    _announce("Starting {} moves, one every {:.1f} s ({:.1f} moves/min), "
              "for about {:.0f} s".format(MOVES, MOVE_PERIOD, MOVES_PER_MINUTE,
                                          duration))

    echo = _start_echo(tgen, duration + 30)
    sender = _start_stream(tgen, duration)
    t_stream = time.time()
    current = ROBOT["home_leaf"]
    moves = []

    try:
        for index in range(MOVES):
            if moves:
                moves[-1]["snapshot_before_next"] = _snapshot(tgen)
            old_leaf = current
            new_leaf = (ROBOT["other_leaf"] if old_leaf == ROBOT["home_leaf"]
                        else ROBOT["home_leaf"])
            before = _mobility_state(tgen)

            watcher = None
            if watch:
                watcher = _ControlPlaneWatcher(
                    _location_checks(tgen, new_leaf), watch_timeout
                )
                watcher.precheck()

            delay = t_stream + PRE_ROLL + index * MOVE_PERIOD - time.time()
            if delay > 0:
                time.sleep(delay)

            if watcher:
                watcher.start()
            t_move = time.time()
            t_move_mono = time.monotonic()
            _roam(tgen, old_leaf, new_leaf)
            roam_ms = (time.time() - t_move) * 1000.0

            control = watcher.finish(t_move_mono) if watcher else {}
            after = _mobility_state(tgen)

            move = {
                "move": index + 1,
                "from": old_leaf,
                "to": new_leaf,
                "t_move": t_move,
                "roam_ms": round(roam_ms, 1),
                "control_plane_ms": control,
                "sequence": "{} {} -> {} {}".format(
                    old_leaf, before.get(old_leaf, {}).get("localSequence"),
                    new_leaf, after.get(new_leaf, {}).get("localSequence"),
                ),
                "mobility_before": before,
                "mobility_after": after,
            }
            moves.append(move)
            _announce("Move {}: {} -> {} done; control plane {}; {}".format(
                index + 1, old_leaf, new_leaf, control or "not watched",
                ", ".join(
                    "{} dup={} count={}".format(
                        leaf, st["duplicate"], st["detectionCount"])
                    for leaf, st in after.items() if leaf in (old_leaf, new_leaf)
                ),
            ))
            current = new_leaf

        if moves:
            moves[-1]["snapshot_before_next"] = _snapshot(tgen)
        stream = _collect_stream(sender, duration)
    finally:
        _stop(sender)
        _stop(echo)

    # Each move's window runs until the next move (or one move period).
    for index, move in enumerate(moves):
        t_from = move["t_move"]
        t_to = t_from + MOVE_PERIOD
        if index + 1 < len(moves):
            t_to = min(t_to, moves[index + 1]["t_move"])
        move["data_plane"] = _analyze(stream, t_from, t_to)
        cp = [ms for leaf, ms in move["control_plane_ms"].items()
              if leaf != move["to"]]
        move["all_leaves_ms"] = (
            max(cp) if cp and all(ms is not None for ms in cp) else None
        )

    RESULTS["moves"] = moves
    if moves:
        RESULTS["pre_move"] = _analyze(
            stream, stream["sent"][0] + 1.0, moves[0]["t_move"]
        )
    path = _save_results(tgen)
    _print_moves_table(moves)
    _announce("Results written to {}".format(path))

    # The robot must still be reachable at its last location.
    _wait_for_robot_at(tgen, current)
    check_ping(CONTROLLER, ROBOT["addr"], True, count=30, wait=1)


def _print_moves_table(moves):
    lines = [
        "Sustained mobility ({:.1f} moves/min, deadline {:.0f} ms)".format(
            MOVES_PER_MINUTE, DEADLINE * 1000
        ),
        "  {:>4} {:<13} {:>9} {:>7} {:>9} {:>11} {:>11} {:<11} {}".format(
            "move", "path", "outage_ms", "missed", "max_run", "all_leaves",
            "leaf5_ms", "sequence", "duplicate flagged on",
        ),
    ]
    def show(value):
        return "-" if value is None else str(value)

    for move in moves:
        dp = move["data_plane"]
        dup = [leaf for leaf, st in move["mobility_after"].items()
               if st["duplicate"]]
        seq = "{}->{}".format(
            move["mobility_before"][move["from"]]["localSequence"],
            move["mobility_after"][move["to"]]["localSequence"],
        )
        lines.append(
            "  {:>4} {:<13} {:>9} {:>7} {:>9} {:>11} {:>11} {:<11} {}".format(
                move["move"],
                "{}->{}".format(move["from"], move["to"]),
                "{}{}".format(dp["outage_ms"], "+" if dp["unrecovered"] else ""),
                dp["missed_instructions"],
                dp["max_consecutive_missed"],
                show(move["all_leaves_ms"]),
                show(move["control_plane_ms"].get("leaf5")),
                seq,
                ", ".join(dup) or "-",
            )
        )
    lines.append("  outage_ms with '+' never recovered before the next move")
    _announce("\n".join(lines))


def test_requirements_verdict():
    "Compare the measurements with the warehouse requirements."
    tgen = get_topogen()
    baseline = RESULTS["baseline"]
    moves = RESULTS["moves"]
    if baseline is None or not moves:
        pytest.skip("Baseline or sustained-mobility measurements are missing")

    deadline_ms = DEADLINE * 1000.0
    verdict = []

    def add(requirement, target, measured, ok):
        verdict.append({"requirement": requirement, "target": target,
                        "measured": measured, "met": ok})

    add(
        "Instruction round trip, robot standing still",
        "every {:.0f} ms, within {:.0f} ms".format(
            INSTRUCTION_PERIOD * 1000, deadline_ms),
        "{} of {} missed, p99 RTT {} ms".format(
            baseline["missed_instructions"], baseline["instructions"],
            baseline["rtt_ms"]["p99"]),
        baseline["missed_instructions"] == 0,
    )

    worst_missed = max(m["data_plane"]["missed_instructions"] for m in moves)
    bad_moves = [m["move"] for m in moves
                 if m["data_plane"]["missed_instructions"] > MAX_MISSED]
    add(
        "Instructions missed per move",
        "<= {}".format(MAX_MISSED),
        "worst {}; over the limit on moves {}".format(
            worst_missed, bad_moves or "none"),
        not bad_moves,
    )

    outages = [m["data_plane"]["outage_ms"] for m in moves]
    add(
        "Service gap per move",
        "< {:.0f} ms".format(INSTRUCTION_PERIOD * 1000),
        "median {} ms, worst {} ms".format(
            _percentile(outages, 50), max(outages)),
        max(outages) < INSTRUCTION_PERIOD * 1000,
    )

    unrecovered = [m["move"] for m in moves if m["data_plane"]["unrecovered"]]
    add(
        "Robot reachable again before its next move",
        "every move",
        "never recovered on moves {}".format(unrecovered or "none"),
        not unrecovered,
    )

    first_dup = next(
        (m["move"] for m in moves
         if any(st["duplicate"] for st in m["mobility_after"].values())),
        None,
    )
    add(
        "{:.0f} moves/min without duplicate-address detection".format(
            MOVES_PER_MINUTE),
        "never flagged",
        "flagged after move {}".format(first_dup) if first_dup else "not flagged",
        first_dup is None,
    )

    RESULTS["verdict"] = verdict
    path = _save_results(tgen)

    lines = ["Warehouse requirements",
             "  {:<46} {:<28} {:<44} {}".format(
                 "requirement", "target", "measured", "result")]
    for row in verdict:
        lines.append("  {:<46} {:<28} {:<44} {}".format(
            row["requirement"], row["target"], row["measured"],
            "MET" if row["met"] else "NOT MET"))
    lines.append("Results written to {}".format(path))
    _announce("\n".join(lines))

    unmet = [row["requirement"] for row in verdict if not row["met"]]
    if ENFORCE:
        assert not unmet, "Requirements not met: {}".format(unmet)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
