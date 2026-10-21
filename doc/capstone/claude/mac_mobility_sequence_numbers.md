# EVPN MAC Mobility: RT-2 Routes, MM Communities, and Sequence Numbers

This explains the `localSequence` and `remoteSequence` values printed by
`show evpn mac vni <vni> mac <mac> json`, which
`test_robot_moves_across_two_leaves()` in
[test_evpn_capstone_mobility.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone_mobility.py)
asserts on. It covers what the two values mean, where FRR calculates them, and
the EVPN concepts behind them (RT-2 routes and the MAC Mobility extended
community).

Line numbers refer to the source tree at the time of writing and may drift.

## Part 1: RT-2s and MM communities

### RT-2: EVPN Route Type 2 (MAC/IP Advertisement route)

EVPN (RFC 7432) uses BGP to carry several kinds of routes, called **route
types**. The capstone fabric uses two of them:

| Type | Name | What it says | In the capstone fabric |
|---|---|---|---|
| **RT-2** | MAC/IP Advertisement | "This MAC (and optionally its IP) is reachable behind me, the VTEP" | One per host, e.g. robot1 |
| RT-5 | IP Prefix | "This subnet is reachable behind me" | 45.0.0.0/24 and 20.0.0.0/24 |

There are other types (RT-1, RT-3 and RT-4, used for multihoming and broadcast
handling), but these two are the ones the capstone tests check.

An RT-2 is how one leaf tells every other leaf where a host is. Two of them
appear in the mobility test output:

```
[2]:[0]:[48]:[00:00:00:00:01:00]                         RD 10.0.0.1:1000
[2]:[0]:[48]:[00:00:00:00:01:00]:[32]:[45.0.0.100]       RD 10.0.0.1:1000
```

Reading the second line field by field:

| Field | Meaning |
|---|---|
| `[2]` | Route type 2 |
| `[0]` | Ethernet Tag ID (unused here) |
| `[48]:[00:00:00:00:01:00]` | MAC address length in bits, then the MAC |
| `[32]:[45.0.0.100]` | IP address length in bits, then the IP |
| `RD 10.0.0.1:1000` | **Route Distinguisher**: makes the route unique per leaf and VNI, so you can tell this one came from leaf1 for VNI 1000 |

The first line is the same route with only the MAC, which is used for
bridging. Each RT-2 also carries:

- the **next hop**: the originating leaf's VTEP IP, which is where to send
  VXLAN traffic
- the **VNI label**
- a set of **extended communities** (next section)

When leaf2 receives this RT-2, zebra installs:

- a **remote MAC entry**: "00:00:00:00:01:00 is behind VTEP 10.0.0.1"
- a matching kernel bridge entry, so frames for that MAC are
  VXLAN-encapsulated to leaf1

Because the route also has the L3VNI attributes, leaf5 additionally installs a
`/32` route for `45.0.0.100` in VRF500. That's the route
`test_evpn_type2_routes_imported` in
[test_evpn_capstone.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone.py)
checks.

### Extended communities, and the MM community

A BGP **community** is a tag attached to a route. An **extended community** is
an 8-byte tag with a type field, so different features can each define their
own. EVPN routes usually carry several:

| Extended community | Purpose |
|---|---|
| **Route Target** (e.g. `RT:1000:1000`) | Decides which VNIs or VRFs import the route. This is what the `route-target import/export` lines in each leaf's `frr.conf` set |
| **Encapsulation** (`ET:8` = VXLAN) | Tunnel type |
| **Router MAC** (`Rmac:…`) | The leaf's MAC, used for routed (L3VNI) traffic |
| **MAC Mobility** (`MM:<seq>`) | The sequence number for host moves |

The **MAC Mobility (MM) extended community** is defined in RFC 7432 §7.7. In
those 8 bytes:

```
type 0x06 | sub-type 0x00 | flags (1 byte; bit 0 = "sticky/static MAC") | reserved | sequence number (4 bytes)
```

Its job is to settle which of two routes for the same MAC is newer. When a
host moves, there can briefly be two RT-2s for its MAC: the old leaf's and the
new leaf's. Both are valid BGP routes from different RDs, so plain BGP can't
tell which one is current. The rule is:

- The **higher MM sequence wins**. A route with no MM community counts as
  sequence 0.
- A leaf that learns a MAC locally while it already has a remote route for
  that MAC must advertise **remote seq + 1**.
- The old leaf receives the higher-sequence route and gives up its local
  entry.

The **sticky flag** marks a MAC that must never move, such as a gateway MAC. A
route claiming to move a sticky MAC is rejected (zebra logs
`EC_ZEBRA_STICKY_MAC_ALREADY_LEARNT`).

The MM community is attached only when the sequence is non-zero. A host that
has never moved has none. In `show bgp l2vpn evpn route …` detail output it
appears as `MM:1`, `MM:2` and so on, next to `RT:…` and `ET:8`. Checking for it
on the new leaf after a move is a quick way to confirm that EVPN treated the
event as a real move.

## Part 2: Where `localSequence` and `remoteSequence` come from

Both numbers are kept in zebra's per-MAC record, one per VNI on each leaf. The
only one that crosses the network is the sequence number carried in the RT-2
route's MAC Mobility extended community.

### Where they're stored and displayed

`show evpn mac vni … json` prints the fields of zebra's `struct zebra_mac`
([zebra_evpn_mac.c:665-666](../../../zebra/zebra_evpn_mac.c#L665-L666)):

```c
json_object_int_add(json_mac, "localSequence", mac->loc_seq);
json_object_int_add(json_mac, "remoteSequence", mac->rem_seq);
```

- **`loc_seq`** is the sequence number *this* leaf uses when it advertises the
  MAC.
- **`rem_seq`** is the sequence number in the best route *received from
  another leaf* for this MAC.

A new entry starts with both at 0 (`zebra_evpn_mac_add`).

### `localSequence`: zebra on the leaf that owns the MAC calculates it

1. **The kernel bridge learns the MAC** on an access port and reports it to
   zebra over netlink. zebra then runs `zebra_evpn_add_update_local_mac()`
   ([zebra_evpn_mac.c:2148](../../../zebra/zebra_evpn_mac.c#L2148)).
2. **zebra decides the value:**
   - **No existing entry:** a new MAC, so `loc_seq` stays at 0
     ([line 2172](../../../zebra/zebra_evpn_mac.c#L2172)).
   - **An existing remote entry:** a real move.
     `loc_seq = MAX(rem_seq + 1, loc_seq)`
     ([lines 2283-2287](../../../zebra/zebra_evpn_mac.c#L2283-L2287)).
   - **An existing local entry whose Ethernet Segment changed** (multihoming
     only): `loc_seq + 1`
     ([line 2249](../../../zebra/zebra_evpn_mac.c#L2249)).
3. **zebra sends the value to bgpd** in a `ZEBRA_MACIP_ADD` message:
   `stream_putl(s, seq)`
   ([zebra_evpn_mac.c:1018](../../../zebra/zebra_evpn_mac.c#L1018)).
4. **bgpd reads it** in `bgp_zebra_process_local_macip()`
   ([bgp_zebra.c:3612](../../../bgpd/bgp_zebra.c#L3612)), then calls
   `bgp_evpn_local_macip_add()` and `update_evpn_route()`.
5. **bgpd attaches it to the route.** If `seq` is non-zero and the MAC isn't a
   gateway MAC, `add_mac_mobility_to_attr()` adds the MAC Mobility extended
   community to the local RT-2
   ([bgp_evpn.c:2330-2331](../../../bgpd/bgp_evpn.c#L2330-L2331)).
   **A sequence of 0 means no community is added at all.**

### `remoteSequence`: bgpd gets it from the received route and passes it to zebra

1. **bgpd receives an RT-2 from a spine** and parses its attributes.
   `bgp_attr_mac_mobility_seqnum()`
   ([bgp_attr_evpn.c:194](../../../bgpd/bgp_attr_evpn.c#L194)) pulls the
   sequence number out of the extended community, or returns 0 if there isn't
   one, and stores it in `attr->mm_seqnum`.
2. **The route goes through normal EVPN best-path selection.** For competing
   RT-2s, the higher sequence number wins.
3. **bgpd installs the winning route into zebra.** `evpn_zebra_install()` reads
   `seq = mac_mobility_seqnum(pi->attr)`
   ([bgp_evpn.c:1542](../../../bgpd/bgp_evpn.c#L1542)) and sends it with
   `bgp_zebra_send_remote_macip()` (`stream_putl(s, seq)` at
   [bgp_evpn.c:1254](../../../bgpd/bgp_evpn.c#L1254)).
4. **zebra decodes it**
   ([zebra_vxlan.c:4493](../../../zebra/zebra_vxlan.c#L4493)), then calls
   `zebra_evpn_rem_macip_add()` and `zebra_evpn_mac_remote_macip_add()`. That
   function stores `mac->rem_seq = seq`
   ([zebra_evpn_mac.c:2142](../../../zebra/zebra_evpn_mac.c#L2142)).
5. **zebra checks the number before accepting the update.** It compares the
   incoming number against the number it already holds: `loc_seq` if the MAC
   is local, `rem_seq` if remote. It accepts the update only if the incoming
   number is the same or higher
   ([zebra_evpn_mac.c:1628-1636](../../../zebra/zebra_evpn_mac.c#L1628-L1636)).
   This is how the old owner gives up the MAC: a remote route with a higher
   sequence turns its local entry into a remote one.

### Putting it together for a real move

```
leaf1 learns MAC locally            → loc_seq=0 → RT-2 has no MM community (seq 0)
leaf2 receives it                    → rem_seq=0, type=remote
robot appears on leaf2 while that
remote entry still exists            → loc_seq = rem_seq+1 = 1 → RT-2 with MM seq 1
leaf1 receives seq 1 ≥ its loc_seq 0 → its local entry becomes remote, rem_seq=1
```

Two more details:

- **Neighbor (IP) entries have their own `loc_seq`/`rem_seq`** in
  `zebra/zebra_evpn_neigh.c`. A MAC+IP RT-2 carries a single sequence number,
  and FRR keeps the MAC and IP sequence numbers in step with each other.
- **`loc_seq` isn't reset when a local MAC becomes remote.** That's why an
  entry can show `type=remote` with a non-zero `localSequence`. That value is
  just left over from earlier; only `remoteSequence` describes the current
  remote route.

## Part 3: Why the mobility test saw sequence 0

In the failing run of `test_robot_moves_across_two_leaves`, `_move_robot()`
unplugged the robot from leaf1 (`nomaster` and `down` on `eth-robot1`) before
plugging it into leaf2. That made leaf1 withdraw its RT-2 first, so leaf2
deleted its remote entry. When the robot then appeared on leaf2, zebra created
a new local entry with sequence 0 instead of treating it as a move. leaf2's
RT-2 therefore had no MM community.

| Scenario | leaf2's RT-2 after the move |
|---|---|
| The test as written (leaf1 withdraws first) | No MM community, which means seq 0. Nothing was competing with it, so there was no need for one |
| A real move (leaf1's route still present) | `MM:1`. It beats leaf1's seq-0 route, and leaf1 switches its entry to remote |

The test's `dest_seq > start_seq` check is really asking whether leaf2 sent an
RT-2 with an MM community higher than leaf1's. To exercise that, the robot has
to be attached to leaf2 and learned there while leaf1 still holds the MAC, and
only then be unplugged from leaf1.
