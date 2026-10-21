# Node Roles: Spines, Leaves, and Endpoint Hosts

The three roles are very different. In this lab only the leaves do real EVPN
work. The spines only carry routes, and the hosts don't run any FRR at all.

## Quick comparison

| | Spines (spine1/2) | Leaves (leaf1–5) | Endpoint hosts (host11…42, controller, robot) |
|---|---|---|---|
| Runs FRR? | Yes (bgpd, zebra) | Yes (bgpd, zebra) | **No**. Plain Linux network namespaces |
| Config | [spine1/frr.conf](../../../tests/topotests/bgp_evpn_capstone/spine1/frr.conf) | [leaf1/frr.conf](../../../tests/topotests/bgp_evpn_capstone/leaf1/frr.conf) … leaf5 | Python only: `tgen.add_host(...)` in `build_topo()` of [test_evpn_capstone.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py) |
| Kernel dataplane | Loopback and point-to-point links only | VRF, bridges, VXLAN, SVI, set up by `_setup_leaf_dataplane()` | One `eth0` with an IP, a fixed MAC and a default route to the anycast gateway |
| EVPN role | Transit only: passes EVPN routes between leaves | VTEP: learns host MAC/IPs, sends and receives RT-2/RT-5 routes, encapsulates/decapsulates VXLAN | Creates the traffic that triggers learning (ARP, ping, GARP) |
| ASN | 65000 (both) | 65011–65015 (one per leaf) | n/a |

## Spines

- **Config:** eBGP unnumbered toward all five leaves
  (`neighbor eth-leafN interface peer-group fabric`), with the `ipv4 unicast`
  and `l2vpn evpn` address families active. They advertise only their
  loopbacks. spine1 and spine2 differ only in their IP and router-id.
- **They have no VNIs, VRF, `advertise-all-vni` or route targets.** A spine
  never installs a MAC or tunnel. It just stores EVPN routes and passes them
  on.
- **Concepts:** eBGP unnumbered (IPv6 link-local with extended-nexthop), BGP
  best-path selection, and update propagation. One subtle point: a spine must
  leave the original VTEP next-hop on EVPN routes it passes on, rather than
  putting its own address there.
- **FRR code paths:** generic bgpd: `bgpd/bgp_route.c` (best path, update and
  withdraw handling), `bgpd/bgp_updgrp*.c` (outbound updates),
  `bgpd/bgp_attr.c`, and `bgpd/bgp_evpn.c` for EVPN prefix parsing.
- **Why spines matter to the project:** every mobility event is a withdraw
  plus an advertise that passes through both spines to every leaf. Spine
  update load and message counts are a natural place to measure BGP churn
  (Phase 3/4 metrics).

## Leaves

All the EVPN behaviour lives here, in two layers.

**1. Kernel dataplane** (set up by Python before FRR starts):

- VRF `VRF500` (table 500), with bridge `br500` and VXLAN device `vni500`.
  This is the L3VNI.
- Bridge `br1000` with `vni1000` (leaf1–4), or `br2000` with `vni2000`
  (leaf5). This is the L2VNI. The bridge is enslaved to the VRF and acts as
  the SVI.
- The SVI has the anycast gateway `45.0.0.1` or `20.0.0.1`, with the same MAC
  `00:00:5e:00:01:01` on every leaf.
- VXLAN devices are created with `nolearning`, so MACs are learned through the
  BGP control plane, not by flooding.
- Host access ports `eth-hostXX` are enslaved to the L2 bridge.

**2. FRR config:**

- The same eBGP underlay toward both spines.
- `advertise-all-vni`, plus an explicit RD and route targets for the L2VNI.
  The route targets are set by hand because each leaf has a different ASN, and
  auto-derived route targets would not match across leaves.
- A `router bgp … vrf VRF500` instance with `advertise ipv4 unicast` and
  `redistribute connected`. This gives symmetric IRB and RT-5 routes for the
  subnets.
- leaf5 uses a **different L2VNI (2000)** from the other leaves, so the
  controller reaches the robots **only by routing over the L3VNI**, not by
  bridging.

**Concepts:** VTEP, L2VNI vs L3VNI, symmetric IRB, anycast gateway, RT-2
(MAC/IP) and RT-5 (IP prefix) routes, RD/RT import and export, ARP/ND
suppression, and **MAC mobility** (the sequence-number extended community from
RFC 7432 §15). The mobility test checks this sequence number:
`localSequence` must increase after a move (see
`test_robot_moves_across_two_leaves()` in `test_evpn_capstone_mobility.py`).

**FRR code paths** (where a mobility-improvement team would work):

- `zebra/zebra_evpn_mac.c`: local and remote MAC handling, mobility sequence
  numbers, duplicate-address detection.
- `zebra/zebra_evpn_neigh.c`: ARP/neighbour (IP) entries tied to MACs.
- `zebra/zebra_evpn.c`, `zebra/zebra_vxlan.c`, `zebra/zebra_vxlan_if.c`,
  `zebra/zebra_l2*.c`: VNI and VTEP state and kernel netlink events (bridge
  FDB, neighbour entries).
- `bgpd/bgp_evpn.c`: building RT-2/RT-5 routes, import into VNIs and VRFs,
  handling the mobility sequence number on receipt.
- `bgpd/bgp_evpn_vty.c`: the `show bgp l2vpn evpn …` commands the route chart
  polls.

## Endpoint hosts

- These are Topogen hosts, not routers. They run no FRR daemons and have no
  config file. Each host has an IP/prefix, a fixed MAC (set in
  `setup_module`), and a default route to the anycast SVI.
- **Robots are modeled as these hosts.** In `test_evpn_capstone_mobility.py`,
  the robot has a link to both leaf1 and leaf2, with only one link up at a
  time. `_move_robot()` simulates a move: it removes the robot's port from the
  old leaf's bridge and brings it down, brings the new port up and adds it to
  `br1000`, then pings the gateway.
- **Concepts:** host behaviour drives learning on the leaves. A leaf only
  learns the robot at its new location once the robot sends a frame (ARP, ping
  or gratuitous ARP). Because the gateway MAC is the same on every leaf, the
  host never needs to re-ARP for its gateway after a move. That is why anycast
  gateway matters for mobility.
- **Relevance to the project:** time-to-reachability depends on how quickly
  the host announces itself after moving. Host-side experiments (GARP right
  after the link comes up, traffic rate, many robots moving at once) are
  things you script in Python on the host namespaces, not in FRR.

## Shared and tooling files

- [test_evpn_capstone.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py):
  builds the fabric and checks that sessions come up, VNIs are present,
  RT-2/RT-5 routes are exchanged, and those routes are imported into VRF500.
- `test_evpn_capstone_mobility.py`: the robot-move test (untracked in git at
  the time of writing).
- [visualizer_server.py](../../../tests/topotests/bgp_evpn_capstone/visualizer_server.py)
  and `templates/`: the topology view and route chart. The route sampler polls
  **leaves only**. If you take the monitoring path, adding spine-side counters
  such as update and withdraw message counts would be a useful extension.

## Summary

The mobility-improvement path is mostly leaf-side zebra/bgpd C code plus
host-side movement scripting. The monitoring path collects data from the
leaves (MAC and sequence state, RT-2 counts), the spines (update churn) and
the hosts (time to regain reachability).
