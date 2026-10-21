# Manual Robot Mobility Walkthrough

This guide reproduces
`tests/topotests/bgp_evpn_capstone/test_evpn_capstone_mobility.py` by hand.
Instead of letting pytest run the assertions, you bring up the same topology,
get an interactive CLI, and type each step of the test yourself: check that
the fabric has converged, move `robot1` from leaf1 to leaf2, and watch EVPN
MAC mobility take effect. Use it to learn what the test is really checking,
to debug a failing run, or to try out your own scenarios before writing them
as a test.

Topology reminder: `robot1` (`45.0.0.100/24`, MAC `00:00:00:00:01:00`) is
cabled to both leaf1 (`eth-leaf1` on the robot, `eth-robot1` on the leaf) and
leaf2 (`eth-leaf2` / `eth-robot1`). Only the leaf1 link is up at the start.
The `controller` (`20.0.0.51`) sits behind leaf5 on L2VNI 2000, so it reaches
the robot through the L3VNI 500 (VRF500).

## 0. Bring up the topology

`--topology-only` runs `setup_module()`: it builds the namespaces, programs
the bridges and VXLAN devices, attaches the robot to leaf1, and starts FRR.
It then drops you into the CLI and skips the test functions.

```bash
cd ~/frr/tests/topotests && sudo -E python3 -m pytest -s --topology-only bgp_evpn_capstone/test_evpn_capstone_mobility.py
```

At the `munet>` prompt:

| Syntax | What it does |
| --- | --- |
| `leaf1 show evpn vni` | Runs a vtysh command on one or more FRR routers (`leaf1 leaf2 show ...`) |
| `robot1 sh ip addr` | Runs a shell command on any node; the only option on plain hosts (`robot1`, `controller`) |
| `vtysh leaf1` / `term robot1` | Opens a full interactive window (needs tmux or X) |
| `help` | Lists the commands |
| `quit` | Tears the whole topology down |

## 1. Wait for the fabric to converge (`_wait_for_evpn_fabric`)

```
munet> spine1 show bgp ipv4 unicast summary
munet> spine1 show bgp l2vpn evpn summary
munet> leaf1 leaf2 leaf5 show evpn vni
```

**Expect:**

- Spines show `eth-leaf1..5` as up, with a prefix count instead of a state
  like `Active` or `Connect`.
- Each leaf lists VNI `500` (L3) plus `1000` (or `2000` on leaf5).

If anything isn't up yet, wait a few seconds and repeat. The test polls these
same commands for up to 90 s.

## 2. Prime the hosts so the leaves learn them (`_prime_controller`, `_prime_robot`)

Hosts send no traffic on their own. Pinging the gateway triggers ARP, so the
leaf learns the host's MAC/IP and sends a Type-2 route for it.

```
munet> controller sh ping -c 2 -W 1 20.0.0.1
munet> robot1 sh ping -c 2 -W 1 45.0.0.1
```

## 3. Check the state before the move

```
munet> leaf1 show evpn mac vni 1000 mac 00:00:00:00:01:00
munet> leaf2 show evpn mac vni 1000 mac 00:00:00:00:01:00
munet> leaf1 show bgp l2vpn evpn route type macip
```

**Expect:**

- leaf1 shows the MAC as `local` on `eth-robot1`. Note its local sequence
  number (normally 0).
- leaf2 shows it as `remote` with remote VTEP `10.0.0.1`.
- The macip output has a Type-2 route for `00:00:00:00:01:00` / `45.0.0.100`
  from RD `10.0.0.1:1000`.

Optionally check the L3 side, which should be a BGP `/32` via `10.0.0.1`:

```
munet> leaf5 show ip route vrf VRF500 45.0.0.100/32
```

## 4. Ping before the move

```
munet> controller sh ping -c 3 45.0.0.100
munet> robot1 sh ping -c 3 20.0.0.51
```

## 5. Move the robot (`_move_robot`)

Run these in order. Each line is one `ip` command from the test.

**Unplug from leaf1:**

```
munet> leaf1 sh ip link set eth-robot1 nomaster
munet> leaf1 sh ip link set eth-robot1 down
munet> robot1 sh ip addr flush dev eth-leaf1
munet> robot1 sh ip link set eth-leaf1 down
```

**Plug into leaf2:**

```
munet> leaf2 sh ip link set eth-robot1 master br1000
munet> leaf2 sh ip link set eth-robot1 up
munet> robot1 sh ip addr add 45.0.0.100/24 dev eth-leaf2
munet> robot1 sh ip link set eth-leaf2 up
munet> robot1 sh ip route replace default via 45.0.0.1
```

The last line is needed because flushing the IP from `eth-leaf1` also removed
the robot's default route.

**Make leaf2 learn the robot:**

```
munet> robot1 sh ping -c 2 -W 1 45.0.0.1
```

## 6. Check the state after the move

```
munet> leaf2 show evpn mac vni 1000 mac 00:00:00:00:01:00
munet> leaf1 show evpn mac vni 1000 mac 00:00:00:00:01:00
munet> leaf2 show bgp l2vpn evpn route type macip
munet> leaf5 show ip route vrf VRF500 45.0.0.100/32
```

**Expect:**

- leaf2 is now `local`, and its local sequence is **higher** than leaf1's was
  in step 3. This is the MAC-mobility proof the test asserts.
- leaf1 is `remote` via `10.0.0.2`.
- The Type-2 route now comes from RD `10.0.0.2:1000` and carries a
  MAC-mobility extended community.
- leaf5's `/32` now points at VTEP `10.0.0.2`.
- There's no duplicate flag.

## 7. Ping after the move

```
munet> controller sh ping -c 3 45.0.0.100
munet> robot1 sh ping -c 3 20.0.0.51
```

Type `quit` when you're done.

## Useful extras

- **Watch the move live:** run `term leaf1` before step 5, then `vtysh` and
  `debug bgp updates` / `debug zebra evpn mh mac` in that window. Or tail the
  logs with `logd leaf1 bgpd`.
- **Capture packets on a node's interface:** `pcap leaf2:eth-spine1` captures
  the VXLAN and BGP traffic.
- **Repeat the move:** run step 5 again with the roles swapped (leaf2 → leaf1,
  `eth-leaf2` → `eth-leaf1`). The sequence number should go up again.
- **Watch the automated test instead:** run it with `--pause` to stop after
  each test, or `--pause-at-end` to inspect the final state before teardown:

```bash
cd ~/frr/tests/topotests && sudo -E python3 -m pytest -s --pause-at-end bgp_evpn_capstone/test_evpn_capstone_mobility.py
```
