# Root Cause: The Robot's IP Is Not Relearned After a Move

This follows on from
[warehouse_requirements_results.md](warehouse_requirements_results.md). On
every leaf1 → leaf2 move in
[test_evpn_capstone_warehouse.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone_warehouse.py),
the controller lost the robot for the whole 12 s until its next move
(requirement 3). This document covers:

1. Why we investigated requirement 3 before requirement 2.
2. What the move trace showed, and where the gap is in FRR's code.
3. A detailed, step-by-step explanation with an analogy.

The trace was taken on 2026-11-05 with `WAREHOUSE_TRACE_MOVE=1`. Line numbers
refer to the source tree at the time of writing and may drift.

## Part 1: Why requirement 3 comes before requirement 2

Requirement 2 is the 160–200 ms outage (3 missed instructions) on moves that
worked. Requirement 3 is the moves that never recovered. Fixing the
consistency problem (requirement 3) first makes sense for three reasons.

**1. Requirement 3 is by far the bigger problem.** A failed move costs about
116 instructions, against 3 for a successful one. Half the moves fail, so
nearly all the missed instructions in the run come from requirement 3.

**2. The "successful" moves aren't clean samples.** In the snapshots, leaf5
had no /32 route to the robot before any leaf2 → leaf1 move started. The
earlier failed move had already removed it, so the controller's traffic was
using the 45.0.0.0/24 fallback route spread across leaf1–4. Those moves only
worked because leaf1 still had its leftover neighbor entry from the start of
the test. The 160–200 ms we measured is the outage for a fabric that was
already partly broken, not for a normal move.

**3. Fixing requirement 3 will change the requirement 2 numbers.** Once every
move relearns the IP properly, each move starts from the normal state: leaf5
has a /32 route to the robot's current leaf. Only then does measuring the
outage give the number we actually care about.

The two problems aren't fully independent. Even a perfectly consistent fabric
will likely still miss about 2–3 instructions per move, but that can't be
measured properly until requirement 3 is fixed.

### Suggested order

1. **Confirm why the new leaf ignores the robot's ARP** (Part 2 below). The
   fix depends on it: the answer decides whether it belongs in the leaf
   (zebra probing for the IP, as RFC 9721 §6.8 suggests), the kernel neighbor
   settings, or the robot (announcing itself again after the move).
2. **Fix it, or work around it, so every move relearns the IP.** Then rerun
   the test and check that requirement 3 passes in both directions.
3. **Then go after requirement 2 on clean moves.** Break the outage into
   stages:
   - the new leaf learns the MAC
   - its update reaches the old leaf and leaf5
   - those leaves reprogram their kernels

   vtysh polling (±100 ms) is too coarse for a 100 ms budget, so this step
   needs timestamps from FRR's debug logs or packet captures.

## Part 2: What the move trace showed

### How the trace was taken

`WAREHOUSE_TRACE_MOVE=N` traces move N. For that move, the test:

- turns on `debug zebra vxlan` and `debug zebra kernel` on the old and new
  leaf;
- records timestamped neighbor events (`ip -ts monitor neigh`) and FDB events
  (`bridge -timestamp monitor fdb`) on both leaves, plus neighbor events on
  the robot;
- saves the starting state: ARP sysctls, the robot's neighbor entry and its
  FDB entries.

Output goes to `trace_moveN_*.txt` in the log directory, and the zebra debug
lines are in `leaf2/zebra.log` and `leaf1/zebra.log`. To reproduce:

```bash
cd ~/frr/tests/topotests && sudo -E WAREHOUSE_MOVES=2 WAREHOUSE_TRACE_MOVE=1 python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_warehouse.py
```

`WAREHOUSE_MOVES` should be even. With an odd number, the robot ends on leaf2,
and `test_sustained_mobility` fails its final reachability check because of
this same bug.

### Timeline of a leaf1 → leaf2 move

| Time after move | Where | What happened |
|---|---|---|
| +77 ms | robot | Sends an ARP for its gateway on the new link and gets an answer, so the ARP **did reach leaf2** |
| +79 ms | leaf2 kernel | Learns the robot's MAC on `eth-host11`, but creates **no neighbor event for 45.0.0.11**. leaf2 already had 45.0.0.11 as a zebra-installed `NOARP` entry pointing at leaf1, and the kernel doesn't replace that from an ARP |
| ~+80 ms | leaf2 zebra | Sends a MAC-only route (`IP (null) seq 1`) to bgpd, then logs "Processing neighbors on local MAC … ADD" and leaves the remote IP entry alone |
| ~+235 ms | leaf1 | Accepts seq 1 and switches the MAC to remote, so its own IP entry turns inactive and it withdraws the MAC+IP route |
| +349 ms | leaf2 | Receives that withdraw, and zebra deletes the remote 45.0.0.11 entry from the kernel (`FAILED`, then `Deleted`) |
| afterwards | everywhere | No leaf has the robot's IP, and the robot doesn't ARP again because its gateway entry is still fresh. leaf5 loses the /32 route, so the controller can't reach the robot |

The leaf2 zebra log for the same window:

```
Rx RTM_NEWNEIGH AF_BRIDGE IF 6 st 0x2 fl 0x0 MAC 00:00:00:00:00:11 nhg 0 vni 0
UPD MAC 00:00:00:00:00:11 intf eth-host11(6) VID 0 -> VNI 1000 curFlags REM
Send MACIP Add f None  state 1 MAC 00:00:00:00:00:11 IP (null) seq 1 L2-VNI 1000 ESI - to bgp
Processing neighbors on local MAC 00:00:00:00:00:11 ADD, VNI 1000
Recv MACIP DEL VNI 1000 MAC 00:00:00:00:00:11 Remote VTEP 10.0.0.1 from bgp
Recv MACIP DEL VNI 1000 MAC 00:00:00:00:00:11 IP 45.0.0.11 Remote VTEP 10.0.0.1 from bgp
Tx RTM_DELNEIGH family ipv4 IF br1000(10) Neigh 45.0.0.11 MAC null flags 0x10 state 0x0 ext_flags 0x0
Del neighbor 45.0.0.11 intf br1000(10) -> L2-VNI 1000
netlink_route_multipath_msg_encode: RTM_DELROUTE 45.0.0.11/32 vrf 7(500)
```

There is no `RTM_NEWNEIGH` for 45.0.0.11 anywhere in it. The kernel never
reported a locally learned IP.

### Where the gap is in FRR

[zebra_evpn_neigh.c:899-904](../../../zebra/zebra_evpn_neigh.c#L899-L904), in
`zebra_evpn_process_neigh_on_local_mac_change()`, runs when a MAC becomes
local:

```c
/* Walk all neighbors and mark any inactive local neighbors as
 * active and/or update sequence number upon a move, and inform BGP.
 * The action for remote neighbors is TBD.
 * NOTE: We can't simply uninstall remote neighbors as the kernel may
 * accidentally end up deleting a just-learnt local neighbor.
 */
```

Zebra re-activates the *local* IP entries tied to the MAC, but does nothing
with remote ones. This explains the direction difference:

- **leaf2 → leaf1 works.** leaf1 still had a local IP entry: its kernel
  neighbor showed `STALE`, not removed. Zebra re-activated it.
- **leaf1 → leaf2 fails.** leaf2 only ever had the remote copy.

So this isn't a test artifact. It's a known gap in FRR. RFC 9721 §6.8
suggests the fix: the new leaf should probe for the IPs of a MAC that just
moved to it. See [rfc_concepts.md](rfc_concepts.md).

Zebra already has part of the plumbing. `DPLANE_NUD_PROBE` is defined in
[zebra_dplane.h:253](../../../zebra/zebra_dplane.h#L253) and translated to
the kernel's `NUD_PROBE` in
[rt_netlink.c:215](../../../zebra/rt_netlink.c#L215), but nothing in zebra
uses it yet. A neighbor in probe state makes the kernel send its own ARP to
the robot, which would relearn the IP right away.

The leaf ARP settings at the start of the move were:

```
net.ipv4.conf.br1000.arp_accept = 0
net.ipv4.neigh.br1000.base_reachable_time_ms = 30000
net.ipv4.neigh.br1000.gc_stale_time = 60
```

### Possible fix directions

From least to most work:

- **Robot side:** the robot announces itself again shortly after a move. This
  only works around the problem. The `WAREHOUSE_REARP_AFTER=2` experiment
  showed every move then recovered at about 2.6 s.
- **Leaf kernel:** neighbor settings such as `arp_accept`. Unlikely to help,
  since the problem is the `NOARP` entry, not unsolicited ARP.
- **Zebra (the real fix):** in the "TBD" branch, put remote neighbors of a
  newly local MAC into probe state. This could become the project's FRR
  contribution.

## Part 3: The root problem explained step by step

The whole failure comes down to one fact: each leaf keeps **two separate
records** for the robot, and after this move only one of them gets updated.

### Background: what each leaf keeps

**Two kinds of address:**

- **MAC address** (`00:00:00:00:00:11`) identifies the robot's network card.
  Switches use it to deliver frames to the right port.
- **IP address** (`45.0.0.11`) is the robot's "name" for routed traffic. The
  controller is on a different subnet, so it sends packets addressed by IP.

**Two tables on every leaf:**

- **The MAC table (bridge FDB):** "this MAC is on port X" or "this MAC is
  behind leaf Y". The bridge fills it in automatically from *any* frame the
  robot sends.
- **The neighbor table (ARP cache):** "IP 45.0.0.11 belongs to MAC …". It
  only gets filled from ARP traffic. A neighbor entry can be:
  - **Self-learned:** the leaf's kernel learned it from ARP with the robot.
  - **Zebra-installed** (`extern_learn NOARP proto zebra`): zebra copied it
    in from another leaf's BGP route. The kernel treats this as zebra's
    property: it won't ARP to check it and won't overwrite it from ARP it
    overhears.

**Three layers on each leaf:**

- **Kernel:** forwards packets and keeps the tables above.
- **zebra:** watches the kernel's tables, tells bgpd what's local, and
  installs what bgpd learned from other leaves.
- **bgpd:** sends and receives RT-2 routes. An RT-2 is either **MAC-only**
  ("this MAC is behind me") or **MAC+IP** ("this MAC *and* this IP are behind
  me").
  - Only **MAC+IP** routes give leaf5 its **/32 route** ("send 45.0.0.11 to
    leaf X").
  - Without that, leaf5 falls back to the **/24 route** ("45.0.0.x is
    somewhere behind leaf1–4").

See [mac_mobility_sequence_numbers.md](mac_mobility_sequence_numbers.md) for
RT-2 routes and sequence numbers.

### The analogy

Picture an office building with five reception desks (the leaves). Each desk
has a clerk (the kernel) and a manager (zebra). The managers phone each other
through a switchboard (bgpd and the spines).

Each desk keeps two notebooks:

- **The face book (MAC table):** "the person with this face is in my hallway"
  or "…is in desk 1's hallway". The clerk updates it any time they see
  someone walk past.
- **The name book (neighbor table):** "the name Robot-11 belongs to this
  face". There are two kinds of entry:
  - **Pencil notes** the clerk wrote after talking to the person themselves.
  - **Stamped notes from head office** that the manager pasted in from
    another desk's phone call. The clerk is told never to change a stamped
    note. Only the manager can.

The controller's mailroom at desk 5 keeps a directory: "letters for Robot-11
go to desk N". It only gets that line when a desk phones in "I have this face
**and** this name". If a desk only says "I have this face", the mailroom
can't tie the name to that desk.

### Step by step

#### Before the move

**What's true:**

- **leaf1:** has the robot's MAC on its port and a self-learned neighbor entry
  for 45.0.0.11. It advertises MAC+IP.
- **leaf2:** has a zebra-installed (stamped) copy of both, pointing at leaf1.
- **leaf5:** has a /32 route to the robot via leaf1.

**Analogy:** Robot-11 sits in desk 1's hallway. Desk 1 has a pencil note for
the name and has phoned it in. Desk 2 has stamped notes saying "Robot-11,
face F, is at desk 1". The mailroom sends Robot-11's letters to desk 1.

#### +77 ms: the robot ARPs on the new link, and leaf2 answers

**What happened:** the robot moved and asked "who has 45.0.0.1?" (its
gateway). leaf2 replied, so the robot's gateway entry on its new interface
became `REACHABLE`.

**What it means:** this rules out the simplest explanation. The robot did
announce itself, and the announcement reached leaf2. That ARP carried both the
robot's MAC and its IP, so leaf2 *had* the information it needed.

**Analogy:** Robot-11 walks up to desk 2 and asks "where's the exit?". In
asking, it says "Hi, I'm Robot-11." The clerk points to the exit.

#### +79 ms: leaf2's kernel learns the MAC but not the IP

**What happened:**

- **The MAC is learned.** The bridge saw a frame from the robot's MAC on
  `eth-host11` and recorded it as local.
- **The IP is not.** The neighbor monitor shows no new entry for 45.0.0.11.
  The entry that already existed was zebra's `NOARP` copy pointing at leaf1,
  and the kernel doesn't replace that kind of entry from ARP.

**What it means:** this is where things first go wrong. The MAC table updates
from any frame, but the IP-to-MAC binding only updates if the kernel is
allowed to write it, and here it isn't.

**Analogy:** the clerk updates the face book: "face F is in my hallway now".
For the name, they find a stamped note, "Robot-11 is at desk 1", and they're
not allowed to change it. So they write nothing, even though Robot-11 just
introduced itself.

#### ~+80 ms: leaf2's zebra advertises the MAC only, and ignores the remote IP

**What happened:**

- **zebra saw the new local MAC.** It found a remote entry for the same MAC,
  so this is a move, and it sent bgpd `MACIP Add … IP (null) seq 1`: a
  MAC-only route with sequence 1.
- **Then it checked the IPs tied to that MAC.** It logged "Processing
  neighbors on local MAC … ADD". For **local** IP entries it would
  re-activate them and advertise them. For **remote** ones the code does
  nothing ("The action for remote neighbors is TBD"). The robot's IP was a
  remote entry, so it stayed as it was.

**What it means:**

- The sequence number works correctly, so the *MAC* move is handled
  properly.
- But zebra only advertises an IP it has as a local entry, and leaf2 has
  none. So no MAC+IP route goes out from leaf2.
- This is the second rule that contributes: zebra has no plan for an IP that
  belongs to a MAC which just moved here.

**Analogy:** the manager sees the face book changed and phones every desk:
"Face F is at desk 2 now, version 1." They don't mention the name, because the
only name note is the stamped one saying desk 1, and the rulebook says
"stamped names when a face arrives: TBD". The manager does nothing about the
name.

#### ~+235 ms: leaf1 gives up the MAC and withdraws the MAC+IP route

**What happened:** leaf1 got leaf2's seq-1 route, which beats its own seq 0.
It:

- switched the MAC to remote via leaf2 (the FDB entry now points at
  `vni1000`);
- marked its IP entry inactive, since the IP's MAC is no longer local;
- withdrew its MAC+IP route.

leaf1's kernel still keeps its self-learned entry (it later shows as
`STALE`). That leftover matters later.

**What it means:** leaf1 is behaving correctly: it shouldn't keep advertising
an IP for a robot that has left. But now nobody advertises the robot's IP.

**Analogy:** desk 1's manager hears "version 1, face F at desk 2", beats their
version 0, and updates the face book. They phone round: "Cancel what I said
about Robot-11 being here." They don't erase the pencil note, though. It sits
in the notebook, marked "not here right now".

#### +349 ms: leaf2 deletes its remote IP entry

**What happened:** leaf2 got leaf1's withdraw, so zebra deleted the remote
45.0.0.11 entry from the kernel (`FAILED`, then `Deleted`).

**What it means:** leaf2 now has no IP entry for the robot at all. A fresh ARP
from the robot would now be recorded, since nothing blocks it any more. But
the robot already ARPed 270 ms earlier, while the entry was still in the way.

**Analogy:** desk 2's manager hears desk 1's cancellation and peels off the
stamped note. The page is blank now, and the clerk could write a pencil note,
but Robot-11 isn't going to introduce itself again soon.

#### After that: the robot can't be reached

**What happened:**

- **leaf5 lost its /32 route.** No leaf advertises MAC+IP for 45.0.0.11, so
  leaf5 falls back to the /24 route spread across leaf1–4. Each packet from
  the controller lands on whichever of those leaves the load-balancing picks.
- **The ARP from that leaf fails.** The leaf has no entry for 45.0.0.11, so it
  ARPs for the robot. On leaf3 the entry stayed `INCOMPLETE`, meaning it
  never got an answer. A likely reason is that the robot's reply goes to the
  gateway MAC, which every leaf owns. leaf2 keeps the reply for itself and
  doesn't record it, because it never asked. This part wasn't traced
  directly, so treat it as likely rather than proven.
- **The robot doesn't ARP again soon.** It already has a fresh entry for its
  gateway, so it won't ARP until that entry ages out. On Linux that takes
  roughly 15–45 s, which is longer than the 12 s between moves.

**What it means:** the robot is fine, the MAC tables are fine, and the
sequence numbers are fine. The only thing missing is the record that ties the
robot's IP to its new leaf. Routed traffic from the controller depends on
exactly that record, and it doesn't come back until the robot happens to ARP
again.

**Analogy:** a letter for Robot-11 reaches the mailroom. The directory no
longer has a "Robot-11 → desk N" line, only "the 45.0.0.x floor is served by
desks 1–4". It sends the letter to, say, desk 3. Desk 3 shouts down the hall,
"Robot-11?". Robot-11 answers toward the front desk, but desk 2 hears the
answer, didn't ask the question, and ignores it. The letter sits undelivered
until Robot-11 happens to come and ask for directions again.

### Why the opposite direction works

On the leaf2 → leaf1 move, leaf1 still has its old self-learned entry (the
pencil note marked "not here right now"). When the robot's MAC becomes local
on leaf1 again, zebra finds a **local, inactive** IP entry. That's the case
the code handles: it re-activates the entry and advertises MAC+IP straight
away. leaf5 gets its /32 route back in about 250 ms.

leaf2 never had a self-learned entry, only the stamped copy. So the same move
in the other direction falls into the "TBD" case.

## The root problem in one sentence

Two reasonable rules each leave the robot's IP to the other:

- **The kernel** won't overwrite zebra's copy from ARP, so control-plane and
  data-plane state don't fight.
- **zebra** has no rule for a remote IP whose MAC has just become local. The
  code says "TBD" and also warns that removing remote entries could clash
  with the kernel.

So when a robot moves to a leaf that only knew its IP second-hand, the MAC
moves and the IP is lost. It stays lost until the robot happens to ARP again.
