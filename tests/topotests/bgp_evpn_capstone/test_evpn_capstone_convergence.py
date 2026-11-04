#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# test_evpn_capstone_convergence.py
# Part of NetDEF Topology Tests
#
# Copyright (c) 2026 by the FRR Capstone Program
#

"""
test_evpn_capstone_convergence.py: Measure EVPN convergence after a host move.

The fabric is the original Capstone topology from test_evpn_capstone.py
(project_topology.pdf): two spines, leaf1..leaf5, host11..host42 and the
controller. The only addition is a standby access link for the moving host,
host11, to its destination leaf:

  * host11:eth0      <-> leaf1:eth-host11   (original link, live at start)
  * host11:eth-leaf2 <-> leaf2:eth-host11   (standby link, down at start)

Both host11 interfaces share host11's MAC, so the move looks like the same
device appearing on a different leaf.

Two moves are timed, one per test:

  1. test_make_before_break_leaf1_to_leaf2: host11 attaches to leaf2 while
     leaf1 still holds its MAC, so EVPN treats it as a MAC move and leaf2
     advertises a higher MAC-mobility sequence (RFC 7432 section 15). The leaf1
     link is removed afterwards. The timer starts when host11 is attached to
     leaf2.

  2. test_break_before_make_leaf2_to_leaf1: host11 disconnects from leaf2
     first, then attaches to leaf1. The timer starts at the disconnect.

For each move, one watcher thread per leaf polls that leaf and records the
first moment its entry for host11 points at the new location:

  * the new leaf:                 host11 MAC is local
  * other leaves in L2VNI 1000:   host11 MAC is remote via the new VTEP
  * leaf5 (L2VNI 2000):           host11 /32 in VRF500 is a BGP route via the
                                  new VTEP (leaf5 has no VNI 1000 MAC table)

Each measured time is an upper bound: it is when the first successful check
finished. The reported resolution is the gap since the previous (failed) check
started, so the real change happened within that window.

Results are printed (run pytest with -s) and written as JSON to
convergence_results.json in the topotest log directory.

Environment variables:
  CONVERGENCE_POLL_INTERVAL   seconds between checks per leaf (default 0.05)
  CONVERGENCE_TIMEOUT         seconds to wait for every leaf (default 30)
  ENABLE_LIVE_ROUTE_GRAPH     "true" to run the visualizer route sampler
                              (default "false": its vtysh polling adds load
                              while timing)
  VISUALIZER_URL              visualizer address (default http://127.0.0.1:5000)
"""

import json
import os
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

# The moving host. "ifaces" maps leaf -> host11 interface cabled to that leaf;
# the leaf side of either link is always eth-host11.
MOVER = {
    "name": "host11",
    "ip": "45.0.0.11/24",
    "addr": "45.0.0.11",
    "mac": "00:00:00:00:00:11",
    "gw": "45.0.0.1",
    "l2vni": 1000,
    "home_leaf": "leaf1",
    "standby_leaf": "leaf2",
    "ifaces": {"leaf1": "eth0", "leaf2": "eth-leaf2"},
}
CONTROLLER_ADDR = "20.0.0.51"

POLL_INTERVAL = max(
    0.01, float(os.environ.get("CONVERGENCE_POLL_INTERVAL", "0.05"))
)
CONVERGENCE_TIMEOUT = float(os.environ.get("CONVERGENCE_TIMEOUT", "30"))

VISUALIZER_URL = os.environ.get("VISUALIZER_URL", "http://127.0.0.1:5000")

ENABLE_LIVE_ROUTE_GRAPH = (
    os.environ.get("ENABLE_LIVE_ROUTE_GRAPH", "false").lower() == "true"
)
ROUTE_SAMPLE_INTERVAL = max(
    0.5, float(os.environ.get("ROUTE_SAMPLE_INTERVAL_SECONDS", "1.0"))
)
_sampler_stop = threading.Event()

HOST_CONFIG_CMD = "ip -br addr; ip -br link; ip route"

# Collected per-move results, written to convergence_results.json.
RESULTS = []


def _announce(msg):
    "Print a status line to the pytest console (needs pytest -s)."
    print("[convergence] {}".format(msg), flush=True)
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

    for spine in SPINES:
        for leaf in LEAVES:
            _viz_post({"action": "ADD_LINK", "source": spine, "target": leaf})
    # The standby link is not drawn until host11 moves onto it.
    for leaf, spec in LEAVES.items():
        for host in spec["hosts"]:
            _viz_post({"action": "ADD_LINK", "source": leaf, "target": host})
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
    "Build the Capstone EVPN fabric plus a standby link for the moving host."

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

    # Standby link for the moving host. _setup_standby_link() keeps its IP on
    # eth0, since topogen would otherwise configure this interface instead.
    standby = MOVER["standby_leaf"]
    tgen.add_link(
        tgen.gears[MOVER["name"]],
        tgen.gears[standby],
        MOVER["ifaces"][standby],
        "eth-{}".format(MOVER["name"]),
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


def _setup_standby_link(tgen):
    "Give the standby interface the mover's MAC and keep both ends down."
    standby = MOVER["standby_leaf"]
    iface = MOVER["ifaces"][standby]
    home_iface = MOVER["ifaces"][MOVER["home_leaf"]]
    tgen.net[standby].cmd_raises("ip link set eth-{} down".format(MOVER["name"]))
    host = tgen.net[MOVER["name"]]
    host.cmd_raises("ip link set {} address {}".format(iface, MOVER["mac"]))

    # topogen puts a host's IP and default route on its alphabetically first
    # interface, and "eth-leaf2" sorts before "eth0". Move them to eth0.
    host.cmd_raises("ip addr flush dev {}".format(iface))
    host.cmd_raises("ip link set {} down".format(iface))
    host.cmd_raises("ip addr replace {} dev {}".format(MOVER["ip"], home_iface))
    host.cmd_raises("ip route replace default via {}".format(MOVER["gw"]))


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

    _setup_standby_link(tgen)

    for rname, router in tgen.routers().items():
        logger.info("Loading router %s", rname)
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))

    tgen.start_router()

    if _viz_publish_topology():
        _viz_publish_configs(tgen)

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


def _mover_mac_info(router):
    "Return the EVPN MAC JSON object for the mover, or None if missing."
    output = router.vtysh_cmd(
        "show evpn mac vni {} mac {} json".format(MOVER["l2vni"], MOVER["mac"]),
        isjson=True,
    )
    if not output:
        return None
    info = output.get(MOVER["mac"], output)
    if not isinstance(info, dict) or "type" not in info:
        return None
    return info


def _mac_is_local(router):
    "Return None if the mover's MAC is local (and not duplicate) on router."
    info = _mover_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for {}".format(router.name, MOVER["mac"])
    if info.get("type") != "local":
        return "{} {} MAC type {} (want local)".format(
            router.name, MOVER["name"], info.get("type")
        )
    if info.get("isDuplicate"):
        return "{} {} MAC marked duplicate".format(router.name, MOVER["name"])
    return None


def _mac_is_remote(router, vtep):
    "Return None if the mover's MAC is remote via vtep (and not duplicate)."
    info = _mover_mac_info(router)
    if info is None:
        return "{} has no EVPN MAC for {}".format(router.name, MOVER["mac"])
    if info.get("type") != "remote":
        return "{} {} MAC type {} (want remote via {})".format(
            router.name, MOVER["name"], info.get("type"), vtep
        )
    if info.get("remoteVtep") != vtep:
        return "{} {} remoteVtep {} (want {})".format(
            router.name, MOVER["name"], info.get("remoteVtep"), vtep
        )
    if info.get("isDuplicate"):
        return "{} {} MAC marked duplicate".format(router.name, MOVER["name"])
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
    """Return {leaf: (description, check)} for "the mover lives on leaf".

    Each check returns None once that leaf's entry for the mover points at
    the given leaf.
    """
    vtep = LEAVES[leaf]["lo"]
    prefix = "{}/32".format(MOVER["addr"])
    checks = {}
    for name, spec in LEAVES.items():
        router = tgen.gears[name]
        if name == leaf:
            checks[name] = ("MAC local", lambda r=router: _mac_is_local(r))
        elif spec["l2vni"] == MOVER["l2vni"]:
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


def _wait_for_mover_at(tgen, leaf):
    "Assert (with retries) that every leaf agrees the mover lives on leaf."
    for name, (desc, check) in _location_checks(tgen, leaf).items():
        _, result = topotest.run_and_expect(check, None, count=90, wait=1)
        assert result is None, "{} ({}): {}".format(name, desc, result)


def _mover_sequence(tgen, leaf):
    "Return the mover's localSequence on leaf (0 if missing)."
    info = _mover_mac_info(tgen.gears[leaf]) or {}
    return int(info.get("localSequence", 0))


class _ConvergenceWatcher:
    """Poll each leaf in its own thread and timestamp when its check passes.

    Times are time.monotonic() values. For each leaf, "t_done" is when the
    first passing check finished, and "resolution" is the time since the
    previous failing check started (the real change happened in between).
    """

    def __init__(self, checks):
        self.checks = checks
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

    def start(self):
        "Verify no check passes yet, then start one polling thread per leaf."
        for name, (desc, check) in self.checks.items():
            assert check() is not None, (
                "{} already satisfies '{}' before the move".format(name, desc)
            )
        deadline = time.monotonic() + CONVERGENCE_TIMEOUT
        for name, (_, check) in self.checks.items():
            thread = threading.Thread(
                target=self._run, args=(name, check, deadline), daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def wait_for(self, name, timeout=CONVERGENCE_TIMEOUT):
        "Block until one leaf's check passes; return True if it did."
        return self.state[name]["done"].wait(timeout)

    def finish(self):
        "Wait for every thread to pass or time out; return failing leaves."
        for thread in self.threads:
            thread.join(CONVERGENCE_TIMEOUT + 5)
        self.stop.set()
        return {
            name: st["last"]
            for name, st in self.state.items()
            if not st["done"].is_set()
        }

    def report(self, t_ref):
        "Return {leaf: (ms since t_ref, resolution ms)} for passed leaves."
        return {
            name: (
                (st["t_done"] - t_ref) * 1000.0,
                st["resolution"] * 1000.0,
            )
            for name, st in self.state.items()
            if st["done"].is_set()
        }


class _Pinger:
    "Run a background ping from the mover so the new leaf keeps seeing frames."

    def __init__(self, tgen):
        self.host = tgen.net[MOVER["name"]]
        self.proc = None

    def __enter__(self):
        self.proc = self.host.popen(
            ["ping", "-i", "0.2", "-W", "1", MOVER["gw"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return False


def _flush_mover_ip(tgen, leaf):
    "Remove the mover's IP from the interface toward leaf; the link stays up."
    iface = MOVER["ifaces"][leaf]
    tgen.net[MOVER["name"]].cmd_raises("ip addr flush dev {}".format(iface))


def _attach_mover(tgen, leaf):
    "Plug the mover into leaf: bridge the access port and configure the host."
    name = MOVER["name"]
    iface = MOVER["ifaces"][leaf]
    host = tgen.net[name]

    tgen.net[leaf].cmd_raises(
        "ip link set eth-{} master {}".format(name, LEAVES[leaf]["l2br"])
    )
    tgen.net[leaf].cmd_raises("ip link set eth-{} up".format(name))
    host.cmd_raises("ip addr replace {} dev {}".format(MOVER["ip"], iface))
    host.cmd_raises("ip link set {} up".format(iface))
    host.cmd_raises("ip route replace default via {}".format(MOVER["gw"]))

    _viz_post({"action": "ADD_LINK", "source": leaf, "target": name})
    _viz_publish_host_config(tgen, name)


def _detach_mover(tgen, leaf):
    "Unplug the mover from leaf: unbridge and down the port, clear the host side."
    name = MOVER["name"]
    iface = MOVER["ifaces"][leaf]
    host = tgen.net[name]

    tgen.net[leaf].cmd_raises("ip link set eth-{} nomaster".format(name))
    tgen.net[leaf].cmd_raises("ip link set eth-{} down".format(name))
    host.cmd_raises("ip addr flush dev {}".format(iface))
    host.cmd_raises("ip link set {} down".format(iface))

    _viz_post({"action": "DEL_LINK", "source": leaf, "target": name})


def _record(tgen, label, old_leaf, new_leaf, t_ref, ref_name, watcher,
            milestones, extra=None):
    "Print a per-leaf timing table and save the move's results to JSON."
    times = watcher.report(t_ref)
    others = [n for n in times if n != new_leaf]
    all_ms = max(times[n][0] for n in others) if others else None

    lines = [
        "{} {} -> {} (t=0 at {})".format(label, old_leaf, new_leaf, ref_name),
        "  {:<8} {:<28} {:>10} {:>14}".format(
            "leaf", "condition", "time_ms", "resolution_ms"
        ),
    ]
    for name, (desc, _) in watcher.checks.items():
        ms, res = times[name]
        lines.append(
            "  {:<8} {:<28} {:>10.1f} {:>14.1f}".format(name, desc, ms, res)
        )
    lines.append(
        "  all leaves updated their remote entries at {:.1f} ms".format(all_ms)
    )
    for mname, mtime in milestones.items():
        lines.append("  milestone {:<22} {:>10.1f} ms".format(
            mname, (mtime - t_ref) * 1000.0
        ))
    if extra:
        for key, value in extra.items():
            lines.append("  {}: {}".format(key, value))
    _announce("\n".join(lines))

    RESULTS.append(
        {
            "move": label,
            "host": MOVER["name"],
            "from": old_leaf,
            "to": new_leaf,
            "t0": ref_name,
            "poll_interval_s": POLL_INTERVAL,
            "per_leaf_ms": {
                name: {"condition": watcher.checks[name][0],
                       "time_ms": round(times[name][0], 1),
                       "resolution_ms": round(times[name][1], 1)}
                for name in watcher.checks
            },
            "all_remote_updated_ms": round(all_ms, 1),
            "milestones_ms": {
                mname: round((mtime - t_ref) * 1000.0, 1)
                for mname, mtime in milestones.items()
            },
            "extra": extra or {},
        }
    )
    path = os.path.join(tgen.logdir, "convergence_results.json")
    with open(path, "w") as f:
        json.dump(RESULTS, f, indent=2)
    _announce("Results written to {}".format(path))


def _check_controller_reachability():
    "Ping both ways between the controller and the mover."
    check_ping("controller", MOVER["addr"], True, count=30, wait=1)
    check_ping(MOVER["name"], CONTROLLER_ADDR, True, count=30, wait=1)


def test_fabric_converged():
    "Bring the fabric up and confirm every leaf sees host11 on leaf1."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    _wait_for_evpn_fabric(tgen)
    _prime_host_adjacencies(tgen)
    _wait_for_mover_at(tgen, MOVER["home_leaf"])
    _check_controller_reachability()
    _announce("Fabric up; every leaf sees {} on {}".format(
        MOVER["name"], MOVER["home_leaf"]
    ))


def test_make_before_break_leaf1_to_leaf2():
    """Move host11 leaf1 -> leaf2, attaching to leaf2 before leaving leaf1.

    t=0 is when host11 is attached to leaf2. leaf1 still holds the MAC when
    leaf2 learns it, so leaf2 must advertise a higher MAC-mobility sequence.
    The leaf1 link is removed only after leaf1 has switched to remote.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    old_leaf, new_leaf = MOVER["home_leaf"], MOVER["standby_leaf"]
    old_seq = _mover_sequence(tgen, old_leaf)

    watcher = _ConvergenceWatcher(_location_checks(tgen, new_leaf))
    watcher.start()
    milestones = {}

    try:
        # Stop using the old link for IP, but keep it up so leaf1 keeps the
        # MAC and EVPN sees a move rather than a new host.
        _flush_mover_ip(tgen, old_leaf)

        t0 = time.monotonic()
        _attach_mover(tgen, new_leaf)
        milestones["attached_to_" + new_leaf] = time.monotonic()

        with _Pinger(tgen):
            assert watcher.wait_for(new_leaf), "{} never learned {}".format(
                new_leaf, MOVER["name"]
            )
            milestones["learned_on_" + new_leaf] = watcher.state[new_leaf]["t_done"]

            # Wait for leaf1 to accept leaf2's higher sequence before
            # unplugging, so leaf1 has no local entry left to withdraw.
            assert watcher.wait_for(old_leaf), "{} never went remote".format(
                old_leaf
            )
            _detach_mover(tgen, old_leaf)
            milestones["detached_from_" + old_leaf] = time.monotonic()

            failed = watcher.finish()
    finally:
        watcher.stop.set()

    assert not failed, "Leaves did not converge: {}".format(failed)

    new_seq = _mover_sequence(tgen, new_leaf)
    _record(
        tgen, "make-before-break", old_leaf, new_leaf, t0,
        "attach to " + new_leaf, watcher, milestones,
        {"sequence": "{} localSequence {} -> {} localSequence {}".format(
            old_leaf, old_seq, new_leaf, new_seq)},
    )
    assert new_seq > old_seq, (
        "MAC mobility sequence did not increase: {} {} -> {} {}".format(
            old_leaf, old_seq, new_leaf, new_seq
        )
    )

    _check_controller_reachability()


def test_break_before_make_leaf2_to_leaf1():
    """Move host11 leaf2 -> leaf1, disconnecting from leaf2 first.

    t=0 is the disconnect from leaf2. host11 is attached to leaf1 right
    after. No sequence assertion: depending on whether leaf2's withdraw
    reaches leaf1 before leaf1 learns host11, EVPN sees either a new MAC
    (sequence 0) or a move (higher sequence). Both are correct.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    old_leaf, new_leaf = MOVER["standby_leaf"], MOVER["home_leaf"]
    _wait_for_mover_at(tgen, old_leaf)

    watcher = _ConvergenceWatcher(_location_checks(tgen, new_leaf))
    watcher.start()
    milestones = {}

    try:
        t0 = time.monotonic()
        _detach_mover(tgen, old_leaf)
        milestones["detached_from_" + old_leaf] = time.monotonic()

        _attach_mover(tgen, new_leaf)
        milestones["attached_to_" + new_leaf] = time.monotonic()

        with _Pinger(tgen):
            assert watcher.wait_for(new_leaf), "{} never learned {}".format(
                new_leaf, MOVER["name"]
            )
            milestones["learned_on_" + new_leaf] = watcher.state[new_leaf]["t_done"]
            failed = watcher.finish()
    finally:
        watcher.stop.set()

    assert not failed, "Leaves did not converge: {}".format(failed)

    _record(
        tgen, "break-before-make", old_leaf, new_leaf, t0,
        "disconnect from " + old_leaf, watcher, milestones,
        {"sequence": "{} localSequence {}".format(
            new_leaf, _mover_sequence(tgen, new_leaf))},
    )

    _check_controller_reachability()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
