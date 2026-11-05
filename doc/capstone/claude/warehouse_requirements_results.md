# Warehouse Requirements: ARP and What the Results Mean

This explains the results of
[test_evpn_capstone_warehouse.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone_warehouse.py),
which checks the Capstone fabric against the warehouse robot requirements:

- Robots exchange instructions with the controller every 100 ms.
- Robots move about five times per minute.

It also covers ARP, which is needed to understand the biggest failure.

The numbers come from a single 10-move run at 5 moves/min on 2026-11-04,
plus a follow-up experiment with re-ARP turned on. They are a first sample,
not a statistical result.

## How the test works

- **Roles:** host11 plays the robot and `controller` (behind leaf5) plays the
  warehouse controller. Instructions are routed across the L3VNI (VRF500),
  the path a real robot's traffic would take.
- **Two access points:** the robot is cabled to leaf1 and leaf2, and both
  links stay bridged and up for the whole run, like real access points that
  stay up when a robot drives away.
- **What a move is:** a move turns the robot's "radio" off on one link and on
  at the other, using a `tc` drop filter on the robot's side. The robot then
  pings its gateway once to announce itself.
- **The instruction stream:**
  [warehouse_traffic.py](../../../tests/topotests/bgp_evpn_capstone/warehouse_traffic.py)
  sends a probe from the controller to the robot every 10 ms, and the robot
  echoes it back. Every 10th probe counts as an instruction. An instruction
  is **missed** if it comes back late (over 100 ms) or not at all.
- **Outage:** the time from the first failed probe to the next successful
  one, accurate to 10 ms.
- **Control-plane times:** the test also polls each leaf with vtysh to
  record when it points at the robot's new location. These times are
  accurate to about ±100 ms.

Run it with:

```bash
cd ~/frr/tests/topotests && sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_warehouse.py
```

### Where the results are

The log directory is inside the `frr-dev` container, not on the VM:

```
/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone_warehouse/
```

| File | What's in it |
|---|---|
| `warehouse_results.json` | All the measurements: settings, steady-state stats, each move (data plane, control plane, sequence numbers, duplicate flags, state snapshots), and the pass/fail table |
| `exec.log` | The test's log, including every `[warehouse]` line |
| `leaf1/`, `leaf2/`, … | Per-router FRR logs |

Each run overwrites this directory. To copy the results out:

```bash
docker cp frr-dev:/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone_warehouse/warehouse_results.json .
```

## What ARP is

ARP (Address Resolution Protocol) turns an **IP address into a MAC address**
on the local network.

- An IP address says *who* a device is: "send this to 45.0.0.11".
- Ethernet frames are delivered by **MAC address**, so before sending, a
  device has to find the MAC that owns that IP.
- It broadcasts an ARP request, "Who has 45.0.0.1? Tell 45.0.0.11", and
  the owner replies, "45.0.0.1 is at 00:00:5e:00:01:01".
- Each device caches the answer in its **neighbor table** (`ip neigh`) for a
  while, so it doesn't have to ask before every packet.

### Why ARP matters in this fabric

- **ARP is how a leaf learns a host's IP.** When the robot ARPs for its
  gateway, the leaf sees the robot's IP-to-MAC binding. The leaf then
  advertises an RT-2 with the MAC *and* the IP, and that is what gives leaf5
  its `/32` route to the robot. See
  [mac_mobility_sequence_numbers.md](mac_mobility_sequence_numbers.md) for
  RT-2 routes.
- **Seeing the MAC alone isn't enough for routing.** Any frame from the robot
  tells the leaf its MAC, but only ARP gives the leaf the IP. Without the IP
  there is no `/32` route, so the controller, which reaches the robot by
  routing through VRF500, can't find it.
- **The robot rarely ARPs.** Once it has its gateway's MAC cached, it doesn't
  ARP again until that entry expires, which takes tens of seconds on Linux.
  So one missed ARP after a move can leave the robot unreachable for a long
  time.

## What each result means

| # | Requirement | Measured | Result |
|---|---|---|---|
| 1 | Instructions every 100 ms, robot standing still | 0 of 190 missed; p99 round trip 3.7 ms | **Met** |
| 2 | No missed instructions when the robot moves | Best case 160–200 ms outage, 3 instructions missed | **Not met** |
| 3 | Robot reachable again before its next move | Lost for the whole 12 s on every leaf1 → leaf2 move | **Not met** |
| 4 | 5 moves/min without duplicate detection | Flagged duplicate from move 5 (about 50 s in) | **Not met** |

### Requirement 1: instructions every 100 ms while standing still (met)

- **Measured:** 0 of 190 instructions missed, and 99% of round trips took
  under 3.7 ms.
- **Meaning:** when nothing moves, the fabric easily handles the instruction
  rate. This rules out the basic path (controller → leaf5 → VXLAN → leaf1 →
  robot) as the problem. Whatever goes wrong happens during moves.

### Requirement 2: no missed instructions when the robot moves (not met)

- **Measured:** even on moves that worked (leaf2 → leaf1), the robot was
  unreachable for 160–200 ms, which means 3 instructions missed per move.
- **Meaning:** this is the best case, and it is still about double the
  100 ms budget. The control-plane times show why. It takes about 250 ms for
  every leaf to learn the robot's new location, as the new RT-2 travels from
  the new leaf through the spines to every other leaf. Until the old leaf
  hears that update, traffic still goes to it and is dropped.
- **What it tells us:** even with no bugs, standard EVPN mobility in this
  setup is too slow for a 100 ms requirement. Improvements would need to cut
  that time, for example by reacting faster or forwarding traffic during the
  move.

### Requirement 3: reachable again before the next move (not met)

- **Measured:** on every leaf1 → leaf2 move (moves 1, 3, 5, 7 and 9), the
  controller never reached the robot again during the 12 s before the next
  move. That is about 116 missed instructions per move.
- **Meaning:** the fabric lost the robot's IP, not its MAC. The per-move
  snapshots show:
  - **leaf2** learned the robot's MAC as local, but had no ARP entry for its
    IP, so it advertised a MAC-only RT-2.
  - **leaf1** withdrew its MAC+IP route once the MAC moved away.
  - **leaf5** lost the `/32` route and fell back to the `45.0.0.0/24`
    subnet route, load-balanced across leaf1–4. The leaf the traffic landed
    on tried to ARP for the robot and never got an answer.
- **Why only one direction failed:** leaf1 still had its own kernel-learned
  neighbor entry for the robot from the start of the test, so moving back to
  leaf1 worked. leaf2 only ever had the copy zebra installed from leaf1's
  route.
- **The likely cause (still a hypothesis):** the robot's one ARP after the
  move reaches the new leaf while that leaf still holds the IP as a remote,
  zebra-installed entry, so no local entry is created. The robot then
  doesn't ARP again for tens of seconds. The exact kernel/zebra mechanism
  hasn't been confirmed.
- **The confirming experiment:** with `WAREHOUSE_REARP_AFTER=2`, the robot
  ARPs again 2 s after each move. Every move then recovered at about 2.6 s,
  in both directions. The fault shows up whenever the new leaf only knows the
  robot's IP as a remote entry.
- **What it tells us:** this is the most serious problem and the one to
  investigate first. RFC 9721 §6.3 and §6.8 describe this kind of stale entry
  and suggest the leaf probe for it rather than wait for the host. See
  [rfc_concepts.md](rfc_concepts.md).

### Requirement 4: 5 moves per minute without duplicate detection (not met)

- **Measured:** both leaves marked the robot's MAC as a duplicate after
  move 5, about 50 s into the run.
- **Meaning:** FRR treats more than 5 moves in 180 s as a sign of a
  misconfiguration, such as two devices sharing a MAC. A robot moving 5 times
  a minute is legitimate, but it looks the same.
- **Today's impact:** by default FRR only flags the MAC and logs a warning,
  so traffic kept flowing. If an operator turns on `freeze`, the leaves stop
  updating the robot's location, and it is stuck at a stale location until
  someone clears it.
- **What it tells us:** the default limit and the warehouse move rate
  conflict. We would have to raise the limit or rework detection for mobile
  devices.

## Summary

- **Standing still:** the fabric meets the requirement easily.
- **Every move:** costs at least 3 instructions.
- **Some moves:** lose the robot completely, because its IP isn't relearned.
- **The move rate:** trips duplicate detection within a minute.

## Caveats

- **One run.** These are a single run plus one experiment. Repeat runs are
  needed for medians and spread.
- **Lab setup.** This is a network-namespace lab. The vtysh polling adds some
  load while timing.
- **The robot is a plain Linux host.** A real robot's ARP behaviour after
  roaming may differ.
- **No scale.** The test measures what happens to one robot per move. A
  small warehouse (100 robots) is about 8 moves/s across the fabric, and a
  large one (50,000 robots) about 4,000 moves/s. That needs a separate load
  test.
