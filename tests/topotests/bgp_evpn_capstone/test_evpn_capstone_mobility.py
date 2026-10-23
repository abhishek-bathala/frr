#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_mobility.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#


"""
test_evpn_capstone_mobility.py: Move a robot host between two EVPN leaves.

The fabric matches project_topology.pdf (same FRR configs as
test_evpn_capstone.py). A dedicated robot endpoint is dual-homed to leaf1 and
leaf2, with only one access link live at a time. Moving the robot means:

  * unplug from the old leaf (down + unbridge the access port),
  * plug into the new leaf (enslave that port to br1000),
  * ping the anycast SVI so the new VTEP learns the MAC/IP and originates a
    Type-2 with a higher MAC-mobility sequence.

The test checks that leaf1/leaf2 swap local vs remote ownership and that the
controller behind leaf5 can still reach the robot.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

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

# Per-leaf description. Original aisle hosts are omitted; the robot is wired
# separately to leaf1 and leaf2. leaf5 still hosts the controller.
LEAVES = {
    "leaf1": {
        "lo": "10.0.0.1",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {},
        "gw": "45.0.0.1",
    },
    "leaf2": {
        "lo": "10.0.0.2",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {},
        "gw": "45.0.0.1",
    },
    "leaf3": {
        "lo": "10.0.0.3",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {},
        "gw": "45.0.0.1",
    },
    "leaf4": {
        "lo": "10.0.0.4",
        "l2vni": 1000,
        "l2br": "br1000",
        "svi": "45.0.0.1/24",
        "hosts": {},
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
ROBOT_L2VNI = 1000

ROBOT = {
    "name": "robot1",
    "ip": "45.0.0.100/24",
    "addr": "45.0.0.100",
    "mac": "00:00:00:00:01:00",
    "gw": "45.0.0.1",
    "start_leaf": "leaf1",
    "dest_leaf": "leaf2",
}

VISUALIZER_URL = os.environ.get("VISUALIZER_URL", "http://127.0.0.1:5000")

ENABLE_LIVE_ROUTE_GRAPH = (
    os.environ.get("ENABLE_LIVE_ROUTE_GRAPH", "true").lower() == "true"
)
ROUTE_SAMPLE_INTERVAL = max(
    0.5, float(os.environ.get("ROUTE_SAMPLE_INTERVAL_SECONDS", "1.0"))
)
_sampler_stop = threading.Event()

HOST_CONFIG_CMD = "ip -br addr; ip -br link; ip route"


def _announce(msg):
    """Print a timestamped status line to the pytest console (needs pytest -s)."""
    line = "[mobility] {}".format(msg)
    print(line, flush=True)
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
        logger.info("Visualizer not reachable at %s, skipping", VISUALIZER_URL)
        return False

    for spine in SPINES:
        _viz_post({"action": "ADD_NODE", "id": spine, "type": "spine"})
    for leaf, spec in LEAVES.items():
        _viz_post({"action": "ADD_NODE", "id": leaf, "type": "vtep"})
        for host in spec["hosts"]:
            _viz_post({"action": "ADD_NODE", "id": host, "type": "host"})
    _viz_post({"action": "ADD_NODE", "id": ROBOT["name"], "type": "host"})

    for spine in SPINES:
        for leaf in LEAVES:
            _viz_post({"action": "ADD_LINK", "source": spine, "target": leaf})
    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            _viz_post({"action": "ADD_LINK", "source": leaf, "target": host})
    # Only the starting access link is live; the move swaps it (_move_robot).
    _viz_post(
        {"action": "ADD_LINK", "source": ROBOT["start_leaf"], "target": ROBOT["name"]}
    )
    return True


def _viz_publish_host_config(tgen, host):
    "Send one host's addresses, links and routes to the visualizer."
    _viz_post(
        {
            "action": "NODE_CONFIG",
            "id": host,
            "config": tgen.net[host].cmd(HOST_CONFIG_CMD),
        }
    )


def _viz_publish_configs(tgen):
    "Send each node's configuration so the visualizer can show it on click."
    for rname, router in tgen.routers().items():
        _viz_post(
            {
                "action": "NODE_CONFIG",
                "id": rname,
                "config": router.vtysh_cmd("show running-config"),
            }
        )
    for spec in LEAVES.values():
        for host in spec["hosts"]:
            _viz_publish_host_config(tgen, host)
    _viz_publish_host_config(tgen, ROBOT["name"])


def _leaf_route_counts(router):
    "Return RT-2, all-EVPN and received-prefix counts for one leaf."
    macip = router.vtysh_cmd(
        "show bgp l2vpn evpn route type macip json", isjson=True
    )
    evpn = router.vtysh_cmd("show bgp l2vpn evpn route json", isjson=True)
    summary = router.vtysh_cmd("show bgp l2vpn evpn summary json", isjson=True)
    pfx_rcvd = sum(
        int(peer.get("pfxRcd", 0)) for peer in summary.get("peers", {}).values()
    )
    return {
        "macip": int(macip.get("numPrefix", 0)),
        "evpn": int(evpn.get("numPrefix", 0)),
        "pfx_rcvd": pfx_rcvd,
    }


def _route_sampler(tgen):
    "Periodically send per-leaf BGP EVPN route counts to the visualizer."
    if not _viz_post({"action": "ROUTE_SAMPLE_RESET"}):
        return

    metrics = ("macip", "evpn", "pfx_rcvd")
    while not _sampler_stop.wait(ROUTE_SAMPLE_INTERVAL):
        sample = {m: {"total": 0, "per_router": {}} for m in metrics}
        for leaf in LEAVES:
            try:
                counts = _leaf_route_counts(tgen.gears[leaf])
            except Exception:  # pylint: disable=broad-except
                # Router may be starting or stopping; skip it this round.
                continue
            for m in metrics:
                sample[m]["per_router"][leaf] = counts[m]
                sample[m]["total"] += counts[m]

        _viz_post(
            {
                "action": "ROUTE_SAMPLE",
                "ts": time.strftime("%H:%M:%S"),
                "metrics": sample,
            }
        )


def build_topo(tgen):
    "Build the Capstone EVPN fabric plus a dual-homed robot."

    for spine in SPINES:
        tgen.add_router(spine)
    for leaf in LEAVES:
        tgen.add_router(leaf)

    for spine in SPINES:
        for leaf in LEAVES:
            tgen.add_link(
                tgen.gears[spine],
                tgen.gears[leaf],
                "eth-{}".format(leaf),
                "eth-{}".format(spine),
            )

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

    tgen.add_host(ROBOT["name"], ROBOT["ip"], "via {}".format(ROBOT["gw"]))
    # First link becomes the host's configured interface (IP + default route).
    tgen.add_link(
        tgen.gears[ROBOT["name"]],
        tgen.gears[ROBOT["start_leaf"]],
        "eth-{}".format(ROBOT["start_leaf"]),
        "eth-{}".format(ROBOT["name"]),
    )
    tgen.add_link(
        tgen.gears[ROBOT["name"]],
        tgen.gears[ROBOT["dest_leaf"]],
        "eth-{}".format(ROBOT["dest_leaf"]),
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


def _setup_robot(tgen):
    "Attach the robot only to the starting leaf; keep the dest access link down."
    name = ROBOT["name"]
    start = ROBOT["start_leaf"]
    dest = ROBOT["dest_leaf"]
    l2br = LEAVES[start]["l2br"]

    tgen.net[start].cmd_raises(
        "ip link set eth-{host} master {l2br}; "
        "ip link set eth-{host} up".format(host=name, l2br=l2br)
    )
    tgen.net[dest].cmd_raises("ip link set eth-{} down".format(name))

    robot = tgen.net[name]
    robot.cmd_raises(
        "ip link set eth-{} address {}".format(start, ROBOT["mac"])
    )
    robot.cmd_raises(
        "ip link set eth-{} address {}".format(dest, ROBOT["mac"])
    )
    robot.cmd_raises("ip link set eth-{} down".format(dest))
    robot.cmd_raises("ip link set eth-{} up".format(start))


def setup_module(mod):
    "Sets up the pytest environment"
    tgen = Topogen(build_topo, mod.__name__)
    _announce("Building topology (spines, leaves, robot, controller)")
    tgen.start_topology()
    _announce("Topology namespaces are up; logdir={}".format(tgen.logdir))

    for leaf, spec in LEAVES.items():
        logger.info("Programming EVPN dataplane on %s", leaf)
        _setup_leaf_dataplane(tgen, leaf, spec)

    for spec in LEAVES.values():
        for host in spec["hosts"]:
            mac = spec["hosts"][host][1]
            tgen.net[host].cmd_raises(
                "ip link set dev eth0 address {}".format(mac)
            )

    _setup_robot(tgen)
    _announce(
        "Robot {} attached to {} only (MAC {} IP {}); {} access link is down".format(
            ROBOT["name"],
            ROBOT["start_leaf"],
            ROBOT["mac"],
            ROBOT["ip"],
            ROBOT["dest_leaf"],
        )
    )

    for rname, router in tgen.routers().items():
        logger.info("Loading router %s", rname)
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))

    tgen.start_router()
    _announce("FRR started on all routers")

    if _viz_publish_topology():
        _viz_publish_configs(tgen)
        _announce("Topology published to visualizer at {}".format(VISUALIZER_URL))

    if ENABLE_LIVE_ROUTE_GRAPH:
        _sampler_stop.clear()
        mod.route_sampler = threading.Thread(
            target=_route_sampler, args=(tgen,), daemon=True
        )
        mod.route_sampler.start()


def teardown_module(mod):
    "Teardown the pytest environment"
    _sampler_stop.set()
    sampler = getattr(mod, "route_sampler", None)
    if sampler:
        sampler.join(timeout=10)
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


def _wait_for_evpn_fabric():
    "Block until underlay, EVPN sessions, and VNIs are present."
    tgen = get_topogen()
    for rname, peers in _expected_peers().items():
        router = tgen.gears[rname]
        _, result = topotest.run_and_expect(
            lambda r=router, p=peers: _peers_established(r, "ipv4 unicast", p),
            None,
            count=90,
            wait=1,
        )
        assert result is None, "Underlay did not converge: {}".format(result)
        _, result = topotest.run_and_expect(
            lambda r=router, p=peers: _peers_established(r, "l2vpn evpn", p),
            None,
            count=90,
            wait=1,
        )
        assert result is None, "EVPN sessions did not converge: {}".format(result)

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

    _announce("Underlay BGP, EVPN sessions, and VNIs are up")


def _prime_robot(tgen):
    "Ping the anycast SVI so the attached leaf learns the robot MAC/IP."
    cmd = "ping -c 2 -W 1 {}".format(ROBOT["gw"])
    output = tgen.net[ROBOT["name"]].cmd(cmd)
    _announce("Robot ARP/learn ping: {} -> {}\n{}".format(ROBOT["name"], ROBOT["gw"], output))


def _prime_controller(tgen):
    spec = LEAVES["leaf5"]
    cmd = "ping -c 2 -W 1 {}".format(spec["gw"])
    output = tgen.net["controller"].cmd(cmd)
    _announce("Controller ARP/learn ping: controller -> {}\n{}".format(spec["gw"], output))


def _dump_robot_mac(router, label):
    "Print local/remote type and MAC-mobility sequence numbers for the robot."
    info = _robot_mac_info(router)
    if info is None:
        _announce("{}: {} has no EVPN MAC entry for {}".format(label, router.name, ROBOT["mac"]))
        return None
    _announce(
        "{}: {} type={} remoteVtep={} localSequence={} remoteSequence={} duplicate={}".format(
            label,
            router.name,
            info.get("type"),
            info.get("remoteVtep", "-"),
            info.get("localSequence"),
            info.get("remoteSequence"),
            info.get("isDuplicate"),
        )
    )
    return info


def _dump_type2(router, label):
    "Print EVPN Type-2 (MAC/IP) text for the robot MAC/IP."
    output = router.vtysh_cmd("show bgp l2vpn evpn route type macip")
    lines = [
        line
        for line in output.splitlines()
        if ROBOT["addr"] in line
        or ROBOT["mac"] in line
        or "Route Distinguisher" in line
        or "Network" in line
    ]
    if not any(ROBOT["addr"] in line or ROBOT["mac"] in line for line in output.splitlines()):
        _announce("{}: {} has no Type-2 for robot".format(label, router.name))
        return
    _announce(
        "{}: {} Type-2 lines for robot:\n{}".format(
            label, router.name, "\n".join(lines)
        )
    )


def _robot_mac_info(router):
    "Return the EVPN MAC JSON object for the robot, or None if missing."
    output = router.vtysh_cmd(
        "show evpn mac vni {} mac {} json".format(ROBOT_L2VNI, ROBOT["mac"]),
        isjson=True,
    )
    if not output:
        return None
    info = output.get(ROBOT["mac"], output)
    if not isinstance(info, dict) or "type" not in info:
        return None
    return info


def _mac_is_local(router):
    info = _robot_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for robot {}".format(router.name, ROBOT["mac"])
    if info.get("type") != "local":
        return "{} robot MAC type {} (want local)".format(
            router.name, info.get("type")
        )
    if info.get("isDuplicate"):
        return "{} robot MAC marked duplicate".format(router.name)
    return None


def _mac_is_remote(router, vtep):
    info = _robot_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for robot {}".format(router.name, ROBOT["mac"])
    if info.get("type") != "remote":
        return "{} robot MAC type {} (want remote via {})".format(
            router.name, info.get("type"), vtep
        )
    remote = info.get("remoteVtep")
    if remote != vtep:
        return "{} robot remoteVtep {} (want {})".format(router.name, remote, vtep)
    if info.get("isDuplicate"):
        return "{} robot MAC marked duplicate".format(router.name)
    return None


def _vrf_host_via_vtep(router, prefix, vtep):
    "Return None if VRF500 has a BGP /32 whose nexthop is vtep."
    output = router.vtysh_cmd(
        "show ip route vrf {} {} json".format(TENANT_VRF, prefix), isjson=True
    )
    for entry in output.get(prefix, []):
        if entry.get("protocol") != "bgp":
            continue
        for nh in entry.get("nexthops", []):
            if nh.get("ip") == vtep or nh.get("interface") == vtep:
                return None
            # EVPN overlay often stores the VTEP as the BGP nexthop.
            if vtep in str(nh):
                return None
        # Fall back: any BGP route for this prefix is enough if JSON shape varies.
        if vtep in str(entry):
            return None
    return "{} did not import {} via {} into {}".format(
        router.name, prefix, vtep, TENANT_VRF
    )


def _move_robot(tgen, old_leaf, new_leaf):
    "Attach the robot to new_leaf, then unplug it from old_leaf."
    name = ROBOT["name"]
    robot = tgen.net[name]
    new_br = LEAVES[new_leaf]["l2br"]
    new_vtep = LEAVES[new_leaf]["lo"]
    old_rtr = tgen.gears[old_leaf]
    new_rtr = tgen.gears[new_leaf]

    _announce("MOVE START: plug {} into {}, then unplug from {}".format(name, new_leaf, old_leaf))

    robot.cmd_raises("ip addr flush dev eth-{}".format(old_leaf))

    _announce("MOVE: robot IP flushed from eth-{}; link to {} still up".format(old_leaf, old_leaf))

    tgen.net[new_leaf].cmd_raises(
        "ip link set eth-{} master {}".format(name, new_br)
    )
    tgen.net[new_leaf].cmd_raises("ip link set eth-{} up".format(name))
    robot.cmd_raises(
        "ip addr add {} dev eth-{}".format(ROBOT["ip"], new_leaf)
    )
    robot.cmd_raises("ip link set eth-{} up".format(new_leaf))
    _announce(
        "MOVE: {} access port in {}; robot IP {} on eth-{}".format(
            new_leaf, new_br, ROBOT["ip"], new_leaf
        )
    )

    robot.cmd("ip route replace default via {}".format(ROBOT["gw"]))
    _announce("MOVE: robot default route via {} on eth-{}".format(ROBOT["gw"], new_leaf))
    _viz_post({"action": "ADD_LINK", "source": new_leaf, "target": name})
    _viz_publish_host_config(tgen, name)

    def _learned_on_new_leaf():
        robot.cmd("ping -c 1 -W 1 {}".format(ROBOT["gw"]))
        return _mac_is_local(new_rtr)

    _, result = topotest.run_and_expect(_learned_on_new_leaf, None, count=30, wait=1)
    assert result is None, result
    _announce("MOVE: {} learned robot MAC locally".format(new_leaf))

    tgen.net[old_leaf].cmd_raises("ip link set eth-{} nomaster".format(name))
    tgen.net[old_leaf].cmd_raises("ip link set eth-{} down".format(name))
    robot.cmd_raises("ip link set eth-{} down".format(old_leaf))
    _announce("MOVE: {} access port down; robot iface eth-{} flushed/down".format(old_leaf, old_leaf))
    _viz_post({"action": "DEL_LINK", "source": old_leaf, "target": name})

    _, result = topotest.run_and_expect(
        lambda: _mac_is_remote(old_rtr, new_vtep), None, count=30, wait=1
    )
    assert result is None, result

    _prime_robot(tgen)
    _announce("MOVE DONE: waiting for EVPN MAC mobility to converge")


def test_robot_moves_across_two_leaves():
    """Attach robot1 to leaf1, then move it to leaf2 and check EVPN follows.

    After the move, leaf2 must own the MAC locally, leaf1 must point at leaf2's
    VTEP, leaf5 must import 45.0.0.100/32 via 10.0.0.2, and the controller must
    still reach the robot.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    _announce("Waiting for EVPN fabric to converge")
    _wait_for_evpn_fabric()
    _prime_controller(tgen)
    _prime_robot(tgen)

    start = ROBOT["start_leaf"]
    dest = ROBOT["dest_leaf"]
    start_vtep = LEAVES[start]["lo"]
    dest_vtep = LEAVES[dest]["lo"]
    host_prefix = "{}/32".format(ROBOT["addr"])

    start_rtr = tgen.gears[start]
    dest_rtr = tgen.gears[dest]
    leaf5 = tgen.gears["leaf5"]

    _announce("Before move: expect robot local on {}, remote on {} via {}".format(
        start, dest, start_vtep
    ))
    _, result = topotest.run_and_expect(
        lambda: _mac_is_local(start_rtr), None, count=90, wait=1
    )
    assert result is None, result
    _, result = topotest.run_and_expect(
        lambda: _mac_is_remote(dest_rtr, start_vtep), None, count=90, wait=1
    )
    assert result is None, result

    before = _dump_robot_mac(start_rtr, "before-move")
    _dump_robot_mac(dest_rtr, "before-move")
    _dump_type2(start_rtr, "before-move")
    start_seq = (before or {}).get("localSequence", 0)
    _announce("Before-move MAC mobility localSequence on {} = {}".format(start, start_seq))

    _announce("Pinging controller <-> robot while robot is on {}".format(start))
    check_ping("controller", ROBOT["addr"], True, count=30, wait=1)
    check_ping(ROBOT["name"], "20.0.0.51", True, count=30, wait=1)
    _announce("Ping OK before move")

    _move_robot(tgen, start, dest)

    _announce("After move: expect robot local on {}, remote on {} via {}".format(
        dest, start, dest_vtep
    ))
    _, result = topotest.run_and_expect(
        lambda: _mac_is_local(dest_rtr), None, count=90, wait=1
    )
    assert result is None, result
    _, result = topotest.run_and_expect(
        lambda: _mac_is_remote(start_rtr, dest_vtep), None, count=90, wait=1
    )
    assert result is None, result

    after = _dump_robot_mac(dest_rtr, "after-move")
    _dump_robot_mac(start_rtr, "after-move")
    _dump_type2(dest_rtr, "after-move")
    dest_seq = (after or {}).get("localSequence", 0)
    _announce(
        "MAC mobility sequence: {} localSequence {} -> {} localSequence {}".format(
            start, start_seq, dest, dest_seq
        )
    )
    assert dest_seq > start_seq, (
        "MAC mobility sequence did not increase: "
        "leaf1 localSequence {} leaf2 localSequence {}".format(start_seq, dest_seq)
    )

    _, result = topotest.run_and_expect(
        lambda: _vrf_host_via_vtep(leaf5, host_prefix, dest_vtep),
        None,
        count=90,
        wait=1,
    )
    assert result is None, result
    _announce("leaf5 imported {} via VTEP {}".format(host_prefix, dest_vtep))

    _announce("Pinging controller <-> robot after move to {}".format(dest))
    check_ping("controller", ROBOT["addr"], True, count=30, wait=1)
    check_ping(ROBOT["name"], "20.0.0.51", True, count=30, wait=1)
    _announce("Ping OK after move; test passed")


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
