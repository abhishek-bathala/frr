#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_ip_relearn_demo.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
test_evpn_capstone_ip_relearn_demo.py: Show on the visualizer why the robot
becomes unreachable after some moves (warehouse requirement 3).

See doc/capstone/claude/ip_relearning_root_cause.md for the full explanation.
In short: when the robot moves to a leaf that only knows its IP as a copy from
another leaf's route, the new leaf learns the robot's MAC but not its IP. The
old leaf withdraws its MAC+IP route, nobody advertises the IP, and leaf5 loses
its /32 route to the robot until the robot happens to ARP again.

The topology and the way the robot moves are the same as in
test_evpn_capstone_warehouse.py: host11 is the robot, cabled to leaf1 and
leaf2, and a move turns its "radio" off on one link and on at the other.

What the visualizer shows:

  * Instructions: the controller pings the robot every 100 ms. Each reply is
    a green dot from the controller to the robot; each missed reply is red.
  * Each VNI 1000 leaf is labelled with where it thinks the robot's MAC and
    IP are. The robot's current leaf has a green border when it has both, and
    a red border when it has the MAC but not the IP.
  * leaf5 is labelled with its route to the robot: a /32 to one leaf, or the
    /24 fallback spread across leaf1-4. Green when the /32 points at the
    robot's leaf, red otherwise.
  * A caption explains each step. Click a leaf to see its raw EVPN state.

Steps:

  1. The robot is on leaf1. Everything is green.
  2. The robot moves to leaf2. leaf2 learns the MAC but not the IP, leaf5
     falls back to the /24 route, and instructions turn red.
  3. The robot moves back to leaf1. leaf1 still has its own old entry for the
     IP, so it recovers right away.
  4. The robot moves to leaf2 again. The same failure.
  5. The robot ARPs again. leaf2 learns the IP and instructions turn green.

To watch it, start the visualizer first (see doc/capstone/student_cookbook.md,
section 12), open http://localhost:5000, then run this test with -s. Without
the visualizer the test still runs and prints the same story to the console.

Environment variables:
  VISUALIZER_URL        visualizer address (default http://127.0.0.1:5000)
  DEMO_HOLD             seconds to hold each step on screen (default 10)
  DEMO_POLL_INTERVAL    seconds between state checks per leaf (default 0.2)
  DEMO_PING_INTERVAL    seconds between instructions (default 0.1)
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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

VTEP_TO_LEAF = {spec["lo"]: name for name, spec in LEAVES.items()}

VISUALIZER_URL = os.environ.get("VISUALIZER_URL", "http://127.0.0.1:5000")
HOLD = float(os.environ.get("DEMO_HOLD", "10"))
POLL_INTERVAL = max(0.05, float(os.environ.get("DEMO_POLL_INTERVAL", "0.2")))
PING_INTERVAL = max(0.01, float(os.environ.get("DEMO_PING_INTERVAL", "0.1")))

# tc filter priority used for the robot's "radio off" drop filters.
RADIO_PREF = 10

# Collected per-step results, written to demo_results.json.
RESULTS = {"steps": [], "transitions": []}
T_START = time.time()


def _announce(msg):
    "Print a status line to the pytest console (needs pytest -s)."
    print("[demo] {}".format(msg), flush=True)
    logger.info(msg)


def _viz_post(event):
    "Best-effort POST of one event to the visualizer; return False if unreachable."
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


def _viz_publish_topology():
    "Send the fabric nodes and links to the visualizer, if it is running."
    if not _viz_post({"action": "RESET"}):
        return False

    for spine in SPINES:
        _viz_post({"action": "ADD_NODE", "id": spine, "type": "spine"})
    for leaf, spec in LEAVES.items():
        _viz_post({"action": "ADD_NODE", "id": leaf, "type": "vtep"})
        for host in spec["hosts"]:
            _viz_post({"action": "ADD_NODE", "id": host, "type": "host"})

    for spine in SPINES:
        for leaf in LEAVES:
            _viz_post({"action": "ADD_LINK", "source": spine, "target": leaf})
    # Only the robot's active access link is drawn.
    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            _viz_post({"action": "ADD_LINK", "source": leaf, "target": host})
    return True


def _caption(title, text):
    "Show a step caption on the visualizer and print it to the console."
    _viz_post({"action": "CAPTION", "title": title, "text": text})
    _announce("{}\n       {}".format(title, text))


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

    # The leaf2 "access point" port is always up and bridged.
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

    if _viz_publish_topology():
        _announce("Topology published to the visualizer at {}".format(VISUALIZER_URL))
    else:
        _announce("Visualizer not reachable at {}; the story is printed here "
                  "instead".format(VISUALIZER_URL))


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


def _single_entry(output, key):
    "Unwrap a vtysh JSON reply that may or may not be keyed by the address."
    if not output:
        return None
    info = output.get(key, output)
    if not isinstance(info, dict) or "type" not in info:
        return None
    return info


def _leaf_view(tgen, leaf):
    """Return what leaf currently knows about the robot, as a small dict.

    VNI 1000 leaves: {"mac": where, "ip": where}, where "where" is "here",
    another leaf's name, or None. "ip" is "here-inactive" for a local entry
    whose MAC is no longer local.
    leaf5: {"prefix": matched prefix, "via": [leaf names]}.
    """
    router = tgen.gears[leaf]
    if LEAVES[leaf]["l2vni"] != ROBOT["l2vni"]:
        output = router.vtysh_cmd(
            "show ip route vrf {} {} json".format(TENANT_VRF, ROBOT["addr"]),
            isjson=True,
        )
        for prefix, entries in output.items():
            for entry in entries:
                if not entry.get("selected"):
                    continue
                via = sorted({
                    VTEP_TO_LEAF.get(nh.get("ip"), nh.get("ip"))
                    for nh in entry.get("nexthops", [])
                    if nh.get("ip") and nh.get("active", True)
                })
                return {"prefix": prefix, "via": via}
        return {"prefix": None, "via": []}

    mac = _single_entry(router.vtysh_cmd(
        "show evpn mac vni {} mac {} json".format(ROBOT["l2vni"], ROBOT["mac"]),
        isjson=True,
    ), ROBOT["mac"])
    arp = _single_entry(router.vtysh_cmd(
        "show evpn arp-cache vni {} ip {} json".format(ROBOT["l2vni"], ROBOT["addr"]),
        isjson=True,
    ), ROBOT["addr"])

    view = {"mac": None, "ip": None}
    if mac:
        view["mac"] = ("here" if mac.get("type") == "local"
                       else VTEP_TO_LEAF.get(mac.get("remoteVtep"), mac.get("remoteVtep")))
    if arp:
        if arp.get("type") == "local":
            view["ip"] = "here" if arp.get("state") == "active" else "here-inactive"
        else:
            view["ip"] = VTEP_TO_LEAF.get(arp.get("remoteVtep"), arp.get("remoteVtep"))
    return view


def _describe(leaf, view, robot_leaf):
    "Return (label, status) for a leaf's view, as drawn on the visualizer."
    if "prefix" in view:
        # Kept to short lines so the label does not run into leaf4's.
        if view["prefix"] is None:
            text = "no route"
        elif view["prefix"].endswith("/32"):
            text = "/32 -> {}".format(", ".join(view["via"]))
        else:
            text = "/{} fallback\n-> leaves {}".format(
                view["prefix"].split("/")[-1],
                ", ".join(v.replace("leaf", "") for v in view["via"]),
            )
        ok = (view["prefix"] or "").endswith("/32") and view["via"] == [robot_leaf]
        return "{}\nrobot IP: {}".format(leaf, text), "ok" if ok else "bad"

    mac = {"here": "here", None: "unknown"}.get(view["mac"], "at " + str(view["mac"]))
    ip = {
        "here": "learned here",
        "here-inactive": "learned here (inactive)",
        None: "none",
    }.get(view["ip"], "copy -> " + str(view["ip"]))
    label = "{}\nMAC: {}\nIP: {}".format(leaf, mac, ip)
    if leaf != robot_leaf:
        return label, "neutral"
    ok = view["mac"] == "here" and view["ip"] == "here"
    return label, "ok" if ok else "bad"


def _leaf_details(tgen, leaf):
    "Return the raw state behind a leaf's label, shown when it is clicked."
    router = tgen.gears[leaf]
    node = tgen.net[leaf]
    parts = ["$ ip neigh show {}".format(ROBOT["addr"]),
             node.cmd("ip neigh show {}".format(ROBOT["addr"])).strip() or "(none)"]
    if LEAVES[leaf]["l2vni"] == ROBOT["l2vni"]:
        for cmd in ("show evpn mac vni {} mac {}".format(ROBOT["l2vni"], ROBOT["mac"]),
                    "show evpn arp-cache vni {} ip {}".format(ROBOT["l2vni"], ROBOT["addr"])):
            parts += ["", "# " + cmd, router.vtysh_cmd(cmd).strip()]
    cmd = "show ip route vrf {} {}".format(TENANT_VRF, ROBOT["addr"])
    parts += ["", "# " + cmd, router.vtysh_cmd(cmd).strip()]
    return "\n".join(parts)


class _StatePoller:
    """Poll every leaf in its own thread and redraw it when its view changes.

    Every change is printed and kept in RESULTS["transitions"], so the story
    can also be followed from the console or the JSON file.
    """

    def __init__(self, tgen):
        self.tgen = tgen
        self.robot_leaf = ROBOT["home_leaf"]
        self.views = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = []

    def _run(self, leaf):
        drawn = None
        while not self.stop.is_set():
            try:
                view = _leaf_view(self.tgen, leaf)
            except Exception:  # pylint: disable=broad-except
                # vtysh may return partial JSON while state is changing.
                self.stop.wait(POLL_INTERVAL)
                continue
            robot_leaf = self.robot_leaf
            label, status = _describe(leaf, view, robot_leaf)
            with self.lock:
                changed = self.views.get(leaf) != view
                self.views[leaf] = view
            if changed:
                RESULTS["transitions"].append({
                    "t": round(time.time() - T_START, 2), "leaf": leaf, "view": view,
                })
                _announce("  {:>6.2f}s  {}".format(
                    time.time() - T_START, label.replace("\n", " | ")))
            if (label, status) != drawn:
                _viz_post({"action": "NODE_STATE", "id": leaf,
                           "label": label, "status": status})
                drawn = (label, status)
                if changed:
                    _viz_post({"action": "NODE_CONFIG", "id": leaf,
                               "config": _leaf_details(self.tgen, leaf)})
            self.stop.wait(POLL_INTERVAL)

    def start(self):
        for leaf in LEAVES:
            thread = threading.Thread(target=self._run, args=(leaf,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def view(self, leaf):
        with self.lock:
            return self.views.get(leaf)

    def wait_for(self, predicate, timeout):
        "Return True once predicate() is true, False after timeout seconds."
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(POLL_INTERVAL)
        return False

    def finish(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(5)


class _InstructionStream:
    """Ping the robot from the controller and draw one dot per instruction.

    ping -O prints "no answer yet" when the next echo is sent before the
    previous one was answered, so a missed instruction is reported within one
    interval. Late replies to those echoes are not drawn.
    """

    SEQ = re.compile(r"icmp_seq=(\d+)")

    def __init__(self, tgen):
        self.tgen = tgen
        self.proc = None
        self.thread = None
        self.stop = threading.Event()
        self.delivered = 0
        self.missed = 0
        self.lock = threading.Lock()

    def _run(self):
        unanswered = set()
        for raw in self.proc.stdout:
            if self.stop.is_set():
                break
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            match = self.SEQ.search(line)
            if not match:
                continue
            seq = int(match.group(1))
            if "no answer yet" in line:
                unanswered.add(seq)
                with self.lock:
                    self.missed += 1
                packet_type = "UNREACH"
            elif "bytes from" in line and seq not in unanswered:
                with self.lock:
                    self.delivered += 1
                packet_type = "REACH"
            else:
                continue
            _viz_post({"action": "PACKET_FLOW", "packet_type": packet_type,
                       "source": CONTROLLER, "target": ROBOT["name"],
                       "vm": "instruction {}".format(seq)})

    def start(self):
        self.proc = self.tgen.net[CONTROLLER].popen(
            ["stdbuf", "-oL", "ping", "-n", "-O", "-i", str(PING_INTERVAL),
             ROBOT["addr"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def counts(self):
        with self.lock:
            return self.delivered, self.missed

    def finish(self):
        self.stop.set()
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.thread.join(5)


def _roam(tgen, poller, old_leaf, new_leaf):
    "Move the robot's radio and IP to new_leaf, then ARP once, as a roaming client does."
    host = tgen.net[ROBOT["name"]]
    old_if = ROBOT["ifaces"][old_leaf]
    new_if = ROBOT["ifaces"][new_leaf]

    host.cmd_raises(
        _radio_cmd(old_if, False) + " && ip addr flush dev {}".format(old_if)
    )
    host.cmd_raises(
        _radio_cmd(new_if, True)
        + " && ip addr replace {} dev {}".format(ROBOT["ip"], new_if)
        + " && ip route replace default via {} dev {}".format(ROBOT["gw"], new_if)
    )
    poller.robot_leaf = new_leaf
    _viz_post({"action": "DEL_LINK", "source": old_leaf, "target": ROBOT["name"]})
    _viz_post({"action": "ADD_LINK", "source": new_leaf, "target": ROBOT["name"]})
    # The new interface has no ARP entry for the gateway, so this sends one ARP.
    host.cmd("ping -c 1 -W 1 {}".format(ROBOT["gw"]))


def _rearp(tgen, leaf):
    "Make the robot ARP for its gateway again on its current link."
    iface = ROBOT["ifaces"][leaf]
    tgen.net[ROBOT["name"]].cmd(
        "ip neigh flush dev {}; ping -c 1 -W 1 {}".format(iface, ROBOT["gw"])
    )


def _settled_after_move(poller, old_leaf, new_leaf):
    "Wait until the old leaf has given up the MAC and the new leaf's IP entry settled."
    def done():
        old = poller.view(old_leaf) or {}
        new = poller.view(new_leaf) or {}
        return old.get("mac") == new_leaf and new.get("ip") in ("here", None)
    poller.wait_for(done, timeout=10)
    # Let leaf5's route catch up.
    time.sleep(1.0)


def _leaf5_route(poller):
    view = poller.view("leaf5") or {}
    return view.get("prefix"), view.get("via", [])


class _Step:
    "Count instructions delivered and missed while one step is on screen."

    def __init__(self, stream, number, title):
        self.stream = stream
        self.number = number
        self.title = title

    def __enter__(self):
        self.start = self.stream.counts()
        return self

    def __exit__(self, *exc):
        delivered, missed = self.stream.counts()
        RESULTS["steps"].append({
            "step": self.number,
            "title": self.title,
            "delivered": delivered - self.start[0],
            "missed": missed - self.start[1],
        })
        return False


def test_fabric_converged():
    "Bring the fabric up and confirm the controller reaches the robot on leaf1."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    _wait_for_evpn_fabric(tgen)
    _prime_host_adjacencies(tgen)
    check_ping(CONTROLLER, ROBOT["addr"], True, count=30, wait=1)
    check_ping(ROBOT["name"], CONTROLLER_ADDR, True, count=30, wait=1)


def test_requirement3_demo():
    "Walk through the IP-relearning failure step by step on the visualizer."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    home, other = ROBOT["home_leaf"], ROBOT["other_leaf"]
    poller = _StatePoller(tgen)
    stream = _InstructionStream(tgen)
    poller.start()
    stream.start()
    observed = {}

    try:
        # Wait for the starting state: leaf5 has a /32 to leaf1.
        poller.wait_for(lambda: _leaf5_route(poller) == ("45.0.0.11/32", [home]), 30)

        title = "1. Robot at leaf1"
        with _Step(stream, 1, title):
            _caption(title,
                     "leaf1 learned the robot's MAC and IP itself and advertises "
                     "both. leaf5 has a /32 route to leaf1, so every instruction "
                     "reaches the robot (green dots).")
            time.sleep(HOLD)

        title = "2. Robot moves to leaf2: the IP is lost"
        with _Step(stream, 2, title):
            _caption(title,
                     "The robot switches to leaf2's access point and ARPs once. "
                     "leaf2 already holds a copy of the robot's IP from leaf1's "
                     "route, so its kernel ignores the ARP.")
            _roam(tgen, poller, home, other)
            _settled_after_move(poller, home, other)
            observed["leaf2_ip_after_first_move"] = (poller.view(other) or {}).get("ip")
            observed["leaf5_after_first_move"] = _leaf5_route(poller)
            if observed["leaf2_ip_after_first_move"] == "here":
                _caption(title,
                         "leaf2 learned the robot's IP this time, so the failure "
                         "did not reproduce on this run.")
            else:
                _caption(title,
                         "leaf2 has the MAC but no IP, so it advertised the MAC "
                         "only. leaf1 gave up the MAC and withdrew its MAC+IP "
                         "route, and leaf2 deleted its copy. Nobody advertises the "
                         "IP now: leaf5 fell back to the /24 route and "
                         "instructions are dropped (red dots).")
            time.sleep(HOLD)

        title = "3. Robot moves back to leaf1: it recovers"
        with _Step(stream, 3, title):
            _caption(title,
                     "leaf1 still has its own old entry for the robot's IP, "
                     "marked inactive. When the MAC comes back, zebra re-activates "
                     "that entry and advertises MAC+IP, so leaf5 gets its /32 "
                     "route back.")
            _roam(tgen, poller, other, home)
            _settled_after_move(poller, other, home)
            time.sleep(HOLD)

        title = "4. Robot moves to leaf2 again: the same failure"
        with _Step(stream, 4, title):
            _caption(title,
                     "leaf2 still only has a copy of the IP, so the same thing "
                     "happens. zebra handles local IP entries when a MAC moves in, "
                     "but its code leaves remote ones as 'TBD'.")
            _roam(tgen, poller, home, other)
            _settled_after_move(poller, home, other)
            observed["leaf2_ip_after_second_move"] = (poller.view(other) or {}).get("ip")
            time.sleep(HOLD)

        title = "5. Robot ARPs again: leaf2 learns the IP"
        with _Step(stream, 5, title):
            _caption(title,
                     "Nothing blocks leaf2 any more, so this ARP creates a local "
                     "entry. leaf2 advertises MAC+IP, leaf5 gets a /32 route to "
                     "leaf2, and instructions get through again. A real robot "
                     "would not ARP again for tens of seconds.")
            _rearp(tgen, other)
            poller.wait_for(
                lambda: _leaf5_route(poller) == ("45.0.0.11/32", [other]), 10
            )
            time.sleep(HOLD)
        observed["leaf5_after_rearp"] = _leaf5_route(poller)

        _caption("Root cause",
                 "The MAC moves but the IP does not. The kernel won't overwrite "
                 "zebra's copy of the IP from an ARP, and zebra has no rule for a "
                 "remote IP whose MAC just became local. The IP is lost until the "
                 "robot happens to ARP again.")
    finally:
        stream.finish()
        poller.finish()

    lines = ["Instructions per step (one every {:.0f} ms)".format(PING_INTERVAL * 1000),
             "  {:<4} {:<46} {:>9} {:>7}".format("step", "title", "delivered", "missed")]
    for step in RESULTS["steps"]:
        lines.append("  {:<4} {:<46} {:>9} {:>7}".format(
            step["step"], step["title"], step["delivered"], step["missed"]))
    reproduced = (observed.get("leaf2_ip_after_first_move") != "here"
                  and observed.get("leaf2_ip_after_second_move") != "here")
    lines.append("Failure reproduced on both leaf1 -> leaf2 moves: {}".format(
        "yes" if reproduced else "no"))
    _announce("\n".join(lines))

    RESULTS["observed"] = observed
    path = os.path.join(tgen.logdir, "demo_results.json")
    with open(path, "w") as f:
        json.dump(RESULTS, f, indent=2)
    _announce("Results written to {}".format(path))

    # The re-ARP must bring the robot back; that is what proves the cause.
    assert observed["leaf5_after_rearp"] == ("45.0.0.11/32", [other]), (
        "leaf5 did not get a /32 route to {} after the robot ARPed again: {}".format(
            other, observed["leaf5_after_rearp"])
    )
    check_ping(CONTROLLER, ROBOT["addr"], True, count=10, wait=1)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
