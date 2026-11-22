# Mobility Reproduction: Findings

Results from
`tests/topotests/bgp_evpn_capstone/test_evpn_capstone_mobility_stress.py`,
following the plan in [reproduction_next_steps.md](reproduction_next_steps.md).
A short what-and-why is in [ip_mobility_problem.md](ip_mobility_problem.md);
the steps of a single move are in [move_breakdown.md](move_breakdown.md).

Only runs saved with full parameters in `summary.json` count as evidence.
At the time of writing that is six stress runs, all break-before-make, in
`~/packet_captures/bbm/` (make-before-break runs go in `~/packet_captures/mbb/`):
`stress_separate` and `stress_shared`, plus the control runs
`stress_delay_0.025s`, `stress_delay_0.05s`, `stress_delay_0.3s` and
`stress_delay_0.5s`.
Earlier runs are disregarded because their parameters weren't all recorded.

**Summary:** when a robot attaches to its new leaf before that leaf has
processed the old leaf's withdraw, the new leaf never learns the robot's IP.
The fabric keeps the robot's MAC but loses its `/32` host route, and traffic
routed to the robot is mostly lost until it ARPs again or moves on. In
`stress_separate` this happened on 14 of 48 break-before-make moves (29%),
and those moves lost 61% of the controller's pings on average. The cause is
confirmed in FRR and Linux source: zebra installs remote host IPs as `NOARP`
kernel entries, the kernel ignores ARP for `NOARP` entries, and zebra
deliberately leaves those entries alone when the host's MAC becomes local (a
"TBD" in zebra since 2018, still present in upstream FRR master). Only routed
traffic is affected: in `stress_shared`, with the controller on the robots'
subnet, the IP was missed on 20 of 48 moves but the controller saw no extra
loss. Control runs that kept the robot off the network a little longer
between leaves (0.05, 0.3 or 0.5 s), so the old leaf's withdraw arrived
first, had every move learn the IP and converge, which confirms the race as
the cause. The withdraw takes a steady ~120 ms (median) to reach the new
leaf, about the same time the robot normally takes to reappear there, which
is why the race is close: an extra ~50 ms won it on every move in one run,
with as little as 18 ms to spare, and an extra 25 ms still lost it once in 48
moves.

**A second, separate problem: every move has an outage longer than 100 ms.**
In the move-outage test (realistic roaming, one robot), every counted move
lost the controller's traffic for 152–276 ms (1–3 instructions at one per
100 ms), even when the IP was learned. Traffic came back within 0–18 ms of the
old leaf learning, through EVPN, that the robot's MAC had moved, which took
132–227 ms after the new leaf learned it. Until then the controller's traffic
still goes to the old leaf. This is EVPN convergence time in this fabric, not
the IP-learning bug, and re-ARPs don't change it. See [The per-move
outage](#second-problem-the-per-move-outage).

**Make-before-break (`stress_mbb`, one run):** the robot's sequence number
rose on every move for all 8 robots, and the IP was missed on 24 of 48 moves:
on 24 of the 28 moves where the new leaf held a remote entry, and on none of
the 20 where it didn't. Moves alternate missed/learned for every robot. On
moves that learned the IP, the longest silence was 165 ms (median), so the
overlap didn't remove the per-move outage; in this test the robot only keeps
both links for ~76 ms. See [Make-before-break](#make-before-break-stress_mbb).

**Make-before-break flip-flop: cause confirmed.** In the move-outage test,
every leaf1 → leaf2 move flip-flopped (91–184 ms outage, +3 sequence),
because the robot answered a flooded ARP on its old link. In one 10-move run
with the robot silent on its old link during the overlap, no move
flip-flopped: +1 sequence per move, and no outage when the old link went
off. A short start-of-move outage in that run came from the test's own
command order. With that fixed, the next 10-move run had no outage on 9 of
10 moves. The tenth lost a single probe (7 ms) while leaf2 resolved the
robot after leaf5 dropped the `/32`, a knock-on of the IP-learning problem.
A 50-move run with the same settings had no flip-flop, +1 sequence per move,
and 0 of 7,177 instructions missed. One probe (2–10 ms) was lost on 5 of 25
leaf1 → leaf2 moves, the same knock-on, and on none in the other direction
apart from a test-side sender stall. At a 5 s gap the results were the
same: 4 of 25 leaf1 → leaf2 moves lost one probe and 0 of 2,384 instructions
were missed. See [50 moves](#robot-silent-on-its-old-link-50-moves), [50
moves at 5 s](#robot-silent-on-its-old-link-50-moves-at-a-5-s-gap),
[Robot silent on its old
link](#robot-silent-on-its-old-link-during-the-overlap-move_outage_mbb_old_txoff)
and [with the command order
fixed](#robot-silent-on-its-old-link-with-the-command-order-fixed).

**Other teams' branches:** guna found the same `NOARP` mechanism on its own.
Three branches carry a zebra patch that stops installing remote host IPs in
the kernel. In lostone's one before/after pair it didn't reduce the misses
(6 of 96 both times), because zebra's delete on the withdraw then removed the
new leaf's own entry. See [Evidence from other teams'
branches](#evidence-from-other-teams-branches).

**Move style of the data:** all runs except `stress_mbb` are
break-before-make (see [Move style of the data so
far](#move-style-of-the-data-so-far)).

## Setup

`stress_separate` and `stress_shared` used the same settings except the
controller subnet. The `stress_delay_*` runs are `stress_separate` plus
`ATTACH_DELAY` set to 0.025, 0.05, 0.3 or 0.5 (see
[Control: `ATTACH_DELAY`](#control-attach_delay)).

| Parameter | Value |
|---|---|
| Fabric | checked-in topology: spine1–2, robot leaves leaf1–4 (L2VNI 1000), leaf5 for the controller |
| Robots | 8 (`45.0.0.101`–`.108`), each cabled to all four robot leaves, one link live at a time |
| Moves | 6 per robot, round-robin, one every 10 s per robot (0.8 moves/s fabric-wide) |
| Move mode | break-before-make (`bbm`): unplug old leaf, then plug new leaf (~70–90 ms apart), robot pings its gateway out the new link |
| DAD | off (`no dup-addr-detection`), per the customer decision |
| Controller pings | every 0.2 s to each robot |
| Visualizer, route sampler | both on |
| Controller subnet | `stress_separate`: checked-in setup, controller `20.0.0.51` on VNI 2000, traffic to robots is **routed** (VRF500 / L3VNI 500). `stress_shared`: leaf5 gets leaf1's config with its own identity, controller `45.0.0.51` on VNI 1000, traffic to robots is **switched** by MAC |

**Measurement:**

- *Convergence:* move start until every leaf has the new location: MAC local
  on the new leaf, remote via the new VTEP on the other robot leaves, and on
  leaf5 the `/32` via the new VTEP (separate) or the MAC (shared). Timed from
  `bridge monitor fdb` and `ip monitor route` events.
- *IP learned* (`ip_learn_s`): when the new leaf's kernel learned the robot's
  IP from the robot (`ip monitor neigh`).
- *Ping loss and longest silence:* over the robot's whole stay at the new
  leaf.
- *Raw events:* every FDB, ARP-table and route change mentioning a robot, in
  `events.csv`.

## Move style of the data so far

| Runs | Move style | Old leaf's port | Robot off the air |
|---|---|---|---|
| `stress_separate`, `stress_shared` | Break-before-make (`MV_MODE=bbm`) | Goes down: old leaf drops the robot's MAC at once | ~70–90 ms |
| `stress_delay_*` | Break-before-make, plus `ATTACH_DELAY` | Goes down | ~70–90 ms plus the delay |
| `move_outage`, `move_outage_rearp_29` | Break-before-make on the robot's side (radio off on the old link, then on at the new one, `move_outage_traffic.py:140-145`) | **Stays up**: old leaf keeps the robot's MAC until EVPN tells it otherwise | 22–121 ms |

| `stress_mbb` (in `~/packet_captures/mbb/`) | **Make-before-break** (`MV_MODE=mbb`): new leaf's port and the robot's new link come up, the robot moves its IP and pings out the new link, then the old leaf's port goes down | Goes down ~76 ms after the new link comes up | None (on both links for ~76 ms) |

Earlier make-before-break stress runs were disregarded because their
parameters weren't saved. The move-outage test has its own make-before-break
mode (`MOVE_OUTAGE_MODE=mbb`), which keeps both access ports up.

**Stress test limitations (why it's on hold for make-before-break):**
- Its `mbb` mode brings the old leaf's access port down ~76 ms after the new
  link comes up. That starts the old leaf's withdraw early, which real
  roaming (access points staying up) wouldn't do, so it isn't a realistic
  make-before-break model.
- Its ping-loss figure counts expected pings as window ÷ `PING_INT`; at small
  intervals (20 ms) the real send rate falls behind and the shortfall reads
  as loss. Longest silence is reliable.
- It has no control over the make-before-break overlap.

It remains the only test that covers scale (many robots, four leaves).

## Results

| | `stress_separate` | `stress_shared` |
|---|---|---|
| Moves converged | 34 / 48 | 48 / 48 |
| New leaf never learned the robot's IP | 14 / 48 (29%) | 20 / 48 (42%) |
| Convergence median / p95 | 0.35 / 0.55 s | 0.27 / 0.34 s |
| Ping loss median / p95 | 6% / 100% | 6% / 24%\* |
| Longest silence, max | 10.0 s | 1.05 s\* |
| BGP UPDATEs per move | 54.2 | 51.7 |

\*`stress_shared`'s worst loss came from a ~1–2 s stall of the whole VM
(below), not from the fabric.

By whether the new leaf learned the robot's IP:

| | Moves | Converged | Avg ping loss | Longest silence (median / max) |
|---|---|---|---|---|
| `stress_separate`, IP learned | 34 | 34 | 5% | 0.30 / 2.23 s |
| `stress_separate`, IP **not** learned | 14 | 0 | **61%** | **9.94 / 10.0 s** |
| `stress_shared`, IP learned | 28 | 28 | 9% | 0.26 / 1.05 s |
| `stress_shared`, IP **not** learned | 20 | 20 | 7% | 0.26 / 1.01 s |

- **In `stress_separate`, every failed move is a move where the IP wasn't
  learned, and vice versa.** All 14 show `leaf5=None` as the leaf that never
  converged. No robot leaf ever failed on the MAC side.
- **A failed move's outage lasts about as long as the robot stays.** Median
  longest silence 9.94 s against a 10 s stay.
- **The shared subnet hides the problem from the controller.** The IP is
  missed just as often, but the controller's traffic is switched on the MAC,
  which converged on every move.

## What goes wrong

Each step below is confirmed in source: this fork, upstream FRR master, and
Linux v6.8 (the kernel this lab runs).

1. When a leaf learns a robot's MAC+IP route from another leaf, zebra
   installs the robot's IP in the kernel as `NOARP` plus `NTF_EXT_LEARNED`
   (`zebra/zebra_dplane.c:5973`, `zebra/zebra_evpn_neigh.c:110`; shown as
   `extern_learn NOARP proto zebra` in `events.csv`). Every robot leaf imports
   VNI 1000, so the leaf a robot moves to already has this entry.
2. The robot attaches to the new leaf and ARPs. A received ARP updates the
   sender's entry through `neigh_event_ns()` without the admin flag
   (`net/ipv4/arp.c:849`, `net/core/neighbour.c:1517`), and the kernel
   ignores non-admin updates to `NOARP` entries
   (`net/core/neighbour.c:1320-1322`). Replies and gratuitous ARPs take the
   same path (`net/ipv4/arp.c:933`). So if the old leaf's withdraw hasn't
   removed the entry yet, the robot's ARP changes nothing.
3. zebra doesn't touch the remote entry when the robot's MAC becomes local.
   It processes only local IP entries on that MAC: "The action for remote
   neighbors is TBD. NOTE: We can't simply uninstall remote neighbors as the
   kernel may accidentally end up deleting a just-learnt local neighbor"
   (`zebra/zebra_evpn_neigh.c:883` onward). This comment first appears in
   commit `f07e1c99d6` (2018-08-20, "EVPN extended mobility support") and the
   function is unchanged in upstream master as of 2026-11-09.
4. The new leaf learns the MAC from the robot's frames and advertises a
   MAC-only Type-2 route. It doesn't advertise a MAC+IP route.
5. The old leaf's ARP entry for the robot goes inactive and it withdraws its
   MAC+IP route. Every leaf then deletes the robot's IP
   (`FAILED proto zebra` → `Deleted`), and leaf5 deletes the `/32`. Now no
   leaf advertises the robot's IP.

**Recovery** needs the new leaf to learn the IP later. In `stress_separate`,
5 moves lost the race but still learned the IP later, 129 ms to 5.1 s after
attach; none did in `stress_shared`. What triggered those late learns isn't
established yet.

**Why the traffic is lost** even though leaf5 still has the `45.0.0.0/24`
route: that route load-balances across all robot leaves, and a leaf the robot
isn't attached to can't resolve it. Its ARP request reaches the robot, but
the reply is addressed to the anycast gateway MAC, which the robot's own leaf
also owns, so the leaf that asked never gets it. This explanation fits the
loss and the repeated `FAILED` ARP entries on non-local leaves, but it hasn't
been confirmed with a packet capture.

### Evidence: a failure needs a remote entry on the new leaf

"Remote entry" means the new leaf held FRR's `NOARP` entry for the robot's IP
when the move started.

| Run | Remote entry, IP learned | Remote entry, IP **not** learned | No remote entry, IP learned | No remote entry, IP **not** learned |
|---|---|---|---|---|
| `stress_separate` | 23 | 14 | 11 | 0 |
| `stress_shared` | 12 | 20 | 16 | 0 |

- **No remote entry, no failure,** in both runs.
- **The race is close.** In the moves that lost it, the new leaf's remote
  entry was removed 8–45 ms after the robot attached (`stress_separate`,
  median 25 ms) or 11–68 ms (`stress_shared`, median 27 ms). In the moves that
  won it outright, the entry was already gone (or went within 6 ms) and the
  IP was learned 2–29 ms after attach.

### Evidence: one failed move

`stress_separate`, move 9, robot1 (`45.0.0.101`) leaf2 → leaf3. Ping loss
98%, longest silence 9.99 s.

| Time after move start | Event |
|---|---|
| +0.017 s | leaf2: robot's MAC deleted (port unplugged) |
| +0.100 s | leaf3: robot's MAC learned locally (robot attached at +0.109 s and ARPed) |
| — | leaf3 never logs a kernel-learned (`STALE`) entry for `45.0.0.101` |
| +0.137 s | leaf1, leaf3, leaf4: `45.0.0.101 FAILED proto zebra` → `Deleted` (about 28 ms too late for leaf3) |
| +0.190 s | leaf5: `Deleted 45.0.0.101 ... via 10.0.0.2`, never replaced |
| +0.23–0.28 s | leaf1, leaf2, leaf4: robot's MAC via `10.0.0.3` (MAC converged) |

### Evidence: a robot's next move always succeeds

A move that misses the IP clears the robot's ARP entry from every leaf, so
nothing can block its next move.

| Run | After a move that missed the IP, next move learned it |
|---|---|
| `stress_separate` | 11 / 11 |
| `stress_shared` | 16 / 16 |

### Evidence: the route sampler shows it fabric-wide

Before the moves every leaf held 18 Type-2 routes (8 robots × MAC and MAC+IP,
plus the controller's 2). During the moves the median was 15–16 and the
minimum 12–13 in both runs: at any moment about 2–3 of the 8 robots had no IP
route in the fabric.

### What decides the race

- **Break-before-make** (all six runs): unplugging the old port starts the
  withdraw, and the robot attaches about 115 ms later (median, from the old
  leaf deleting its MAC; 140–661 ms in the delay runs). The withdraw takes ~120 ms (see "Withdraw latency" below), so
  without a delay the two overlap: it cleared the new leaf's remote entry in
  time on 18 of the 37 moves where one existed in `stress_separate`, and was
  8–45 ms late on the 14 that failed.
- **Make-before-break** (not measured in these runs): the old leaf would only
  withdraw after the new leaf has advertised the robot's MAC, which happens
  when the robot's first frame (its ARP) arrives. By that reasoning the race
  would be lost whenever the new leaf holds a remote entry.
- **A real warehouse is likely closer to make-before-break.** Access points
  stay connected to their leaves, so the old leaf never sees a port go down;
  it keeps the robot's MAC until the new leaf's higher-sequence route arrives
  or the bridge entry ages out (300 s by default,
  `include/linux/if_bridge.h:65`). This is an inference from the wiring, not
  a measurement.

## Control: `ATTACH_DELAY`

If the race explains the failures, a robot that stays off the network long
enough for the old leaf's withdraw to arrive should never lose its IP. The
delay runs test that: the same settings as `stress_separate`, but the robot
unplugs from the old leaf, waits `ATTACH_DELAY` seconds, then plugs into the
new one and ARPs.

| | `stress_separate` | `stress_delay_0.025s` | `stress_delay_0.05s` | `stress_delay_0.3s` | `stress_delay_0.5s` |
|---|---|---|---|---|---|
| `ATTACH_DELAY` | 0 | 0.025 s | 0.05 s | 0.3 s | 0.5 s |
| Robot arrival after the old leaf deletes its MAC (median, range) | 115 ms (89–311) | 140 ms (119–221) | 169 ms (145–233) | 420 ms (390–534) | 661 ms (591–959) |
| New leaf held a remote `NOARP` entry at move start | 37 / 48 | 47 / 48 | **48 / 48** | **48 / 48** | **48 / 48** |
| New leaf never learned the IP | 14 | **1** | **0** | **0** | **0** |
| Moves converged | 34 / 48 | 47 / 48 | **48 / 48** | **48 / 48** | **48 / 48** |
| Remote entry removed, relative to robot attach (median, range) | +25 ms (+8 to +45) on failed moves | −21 ms (−98 to +15) | −48 ms (−91 to −18) | −294 ms (−421 to −251) | −521 ms (−782 to −447) |
| IP learned after attach (median, range) | — | 6 ms (2–249) | 7 ms (4–22) | 7 ms (2–24) | 7 ms (3–64) |
| Ping loss avg / max | — / 100% | 8% / 100% | 6% / 12% | 7% / 13% | 7% / 13% |
| Longest silence, median / max | — / 10.0 s | 0.32 / 10.0 s | 0.28 / 0.39 s | 0.46 / 0.65 s | 0.71 / 1.03 s |
| leaf5 Type-2 routes during moves (median, of 18) | 15 | 18 | **18** | **18** | **18** |
| BGP UPDATEs per move | 54.2 | 59.6 | 60.0 | 60.0 | 60.0 |
| Convergence median / p95 | 0.35 / 0.55 s | 0.32 / 0.42 s | 0.37 / 0.43 s | 0.63 / 0.73 s | 0.87 / 1.12 s |

(Arrival is measured from the old leaf's MAC deletion in `events.csv`, which
is comparable across runs. The test's own `detach_s`/`attach_s` timestamps
are taken at different points with and without a delay, so their difference
isn't.)

- **From 0.05 s up, every move started with the blocking entry and none
  failed.** The entry was always gone before the robot attached, so its ARP
  arrived with nothing in the way and its IP was learned within a few ms.
- **At 0.025 s the margin ran out once.** Arrival moved to 140 ms (median)
  against withdraws of 116–167 ms, so the two still overlapped on a few
  moves. One move (id 40, robot8 leaf4 → leaf1) lost the race: its withdraw
  took 136 ms, slower than usual, while the robot arrived early (124 ms). It
  missed the IP, lost 100% of pings and was silent for its whole 10 s stay,
  exactly like the failures in `stress_separate`.
- **0.05 s was enough, but only just.** The delay shifted the robot's arrival
  by about the delay itself (169 ms against 115 ms), which put it after every
  withdraw in that run (slowest 144 ms). The closest move had the entry gone
  only 18 ms before the robot attached. One run, at one load: a slower
  withdraw would eat that margin.
- **Every move had the entry because none missed.** With no misses, nothing
  cleared the robots' entries between moves (the reverse of the
  next-move-always-succeeds pattern above).
- **The cost is a fixed outage instead of the race, and it scales with the
  delay.** The robot is off the network for the delay: longest silence
  medians were 0.32, 0.28, 0.46 and 0.71 s (0.025, 0.05, 0.3, 0.5 s). At 0.05 s that's no worse than a
  converged move without a delay, while removing the up-to-10 s outages.
  Convergence and reachability gap include the delay because both are
  measured from the move start.
- **No sequence bumps** (`robots_seq_counts_moves` = 0 in all four): the
  withdraw always arrived first, so the new leaf never saw the robot as remote
  when learning it.
- **Not a fix.** The delay only helps break-before-make, and only because
  unplugging the old port starts the withdraw, which a real warehouse wouldn't
  do. It confirms the cause; zebra and the kernel are unchanged.

### Withdraw latency against robot arrival

The race comes down to two times, both measured from the moment the old leaf
deletes the robot's MAC (from `events.csv`):

- **Withdraw latency:** until the new leaf drops its remote IP entry for the
  robot (`Deleted <ip> … proto zebra`). This covers zebra and bgpd on the old
  leaf, both spines, and bgpd and zebra on the new leaf.
- **Robot arrival:** until the robot attaches to the new leaf (its ARP follows
  within a few ms).

| Run | Moves measured | Withdraw latency median / p95 (min–max) | Robot arrival median (min–max) |
|---|---|---|---|
| `stress_separate` | 37 | 122 / 149 ms (113–164) | 115 ms (89–268) |
| `stress_shared` | 32 | 125 / 136 ms (111–145) | 107 ms (77–279) |
| `stress_delay_0.025s` | 47 | 121 / 144 ms (116–167) | 140 ms (119–221) |
| `stress_delay_0.05s` | 48 | 122 / 136 ms (114–144) | 169 ms (145–233) |
| `stress_delay_0.3s` | 48 | 123 / 155 ms (113–192) | 420 ms (390–534) |
| `stress_delay_0.5s` | 48 | 129 / 169 ms (116–176) | 661 ms (591–959) |

("Moves measured" are the moves where the new leaf held a remote entry, so
there was a withdraw to observe.)

- **The withdraw takes a steady ~120–130 ms** in every run, with a narrow
  spread, and it didn't drift over the course of a run (medians by thirds of
  each run: 120–136 ms).
- **With no delay, the robot reappears at about the same time** (~107–115 ms
  median), so the two overlap and either can come first. That's why the race
  was lost on 14 of 37 such moves in `stress_separate`.
- **With 0.05 s or more of delay, the robot arrived after every withdraw in
  its run,** so the race was never lost. At 0.05 s the margin was small
  (arrival 145–233 ms against withdraws up to 144 ms); at 0.025 s it ran out
  once (below).
- **The withdraw latency was the same on moves that learned the IP and moves
  that missed it** (`stress_separate`: 124 against 122 ms median). What
  decided each move was when the robot arrived, not a slow withdraw.
- **All six runs used the same load** (8 robots, 0.8 moves/s), so this
  doesn't yet show whether the withdraw slows down with more moves. That
  needs runs with more robots or a shorter `MV_INT`.

### Race margin per move

Per move, the **race margin** is the time from the new leaf dropping its
remote entry to the robot attaching (positive: the withdraw won; negative:
the robot got there first). Only moves where the new leaf held a remote entry
have one.

| Run | `ATTACH_DELAY` | Moves with a margin | Negative margins | Smallest margin | Median margin | IP missed |
|---|---|---|---|---|---|---|
| `stress_separate` | 0 | 37 | 24 | −61 ms | −6 ms | 14 |
| `stress_delay_0.025s` | 0.025 s | 47 | 3 | −15 ms | +21 ms | 1 |
| `stress_delay_0.05s` | 0.05 s | 48 | 0 | +18 ms | +48 ms | 0 |
| `stress_delay_0.3s` | 0.3 s | 48 | 0 | +251 ms | +294 ms | 0 |
| `stress_delay_0.5s` | 0.5 s | 48 | 0 | +447 ms | +521 ms | 0 |

- **A negative margin doesn't always mean a miss.** The robot's ARP goes out a
  few ms after the test's attach timestamp, so a margin just below zero can
  still be won (id 24 in the 0.025 s run, −5 ms, learned the IP). A lost race
  can also recover later (id 28, −15 ms, learned it ~250 ms later, as 5 moves
  did in `stress_separate`). Misses are a subset of negative margins.
- **The threshold sits between +25 and +50 ms of extra delay** at this load:
  the margin distribution moves up by about the delay, and the race stops
  being lost once its low end clears zero. Medians alone would hide this; the
  smallest margin and the count of negative ones show it.

## Other findings

- **Controller on the robots' subnet.** With `CTRLER_SUBNET=shared`,
  convergence was faster (0.27 s against 0.35 s median) because leaf5's check
  is its MAC table, which updates before its VRF `/32`. leaf5 still installs
  robot `/32` routes in VRF500 even though it has the subnet locally.
- **VM stall in `stress_shared`.** The eight worst moves (ids 24–31, one per
  robot, 17–24% loss with ~1 s gaps) all overlap ~39–40 s into the run, where
  the test's own move loop stalled (move 32 started 0.83 s late, against 1–5
  ms normally). Fabric event counts in that window were normal.
- **BGP UPDATEs per move follow the miss rate:** 60.0 in the delay runs with no
  misses and 59.6 in the 0.025 s run (one miss), 54.2 in `stress_separate` (29% missed), 51.7 in `stress_shared`
  (42% missed). A move that misses the IP never sends its MAC+IP route, so it
  costs fewer UPDATEs; a lower UPDATEs-per-move figure here means more
  failures, not better efficiency. leaf5 sent as many UPDATEs as each robot
  leaf (260 in `stress_separate`) though it originates no robot routes. A
  likely reason is leaves passing each robot route back up to the other spine,
  which rejects it as an AS-path loop; not confirmed.
- **Route sampler load.** One polling round (3 vtysh commands × 5 leaves)
  took about 1.2 s and repeated every ~2.2 s, so the leaves' bgpd was queried
  about half the time. bgpd runs `show` commands on the same main thread that
  processes updates (it starts only I/O and keepalive pthreads,
  `bgpd/bgpd.c:9693-9694`), so the sampler could delay withdraws. All six
  runs had it on, so its effect is unknown.

## Move-outage test with realistic roaming (one run)

`test_evpn_capstone_move_outage.py`:
one robot alternating leaf1 ↔ leaf2, 4 moves 15 s apart, robot-side "radio"
so both access ports stay up (the old leaf only gives the robot up when the
new leaf's higher-sequence route arrives), 10 ms probes, VXLAN data-plane
learning off, and its ARP workaround turned off (`MOVE_OUTAGE_REARP_COUNT=0`).
Results in `~/packet_captures/bbm/move_outage`. This is a different test from the
stress test, run once, so it's supporting evidence rather than one of the
stress runs above.

| Move | Outage (fabric part) | Instructions missed | New leaf learned the IP? | leaf5 `/32` afterwards |
|---|---|---|---|---|
| 1 leaf1 → leaf2 | 152 ms (133) | 1 | yes, +391 ms | via leaf2 at +582 ms |
| 2 leaf2 → leaf1 | 276 ms (248) | 2 | **no** | **withdrawn**, not replaced |
| 3 leaf1 → leaf2 | not counted (leaf5 had no `/32` at the start) | 1 | via leftover entry | via leaf2 at +363 ms |
| 4 leaf2 → leaf1 | 199 ms (142) | 2 | **no** | **withdrawn**, not replaced |

- **Every counted move had an outage over the 100 ms budget.** It lasted
  until the old leaf heard, through EVPN, that the robot's MAC had moved
  (132–226 ms after the new leaf learned it). Until then leaf5 still sent the
  robot's traffic to the old leaf. This is the problem the test was built to
  show, and it's separate from the IP-learning failure.
- **Moves to leaf1 lost the robot's IP, as predicted for this move style.**
  leaf1 held zebra's remote `NOARP` copy of the robot's IP (it had replaced
  leaf1's own entry during move 1), so the robot's ARP was ignored. leaf1
  never advertised MAC+IP, and leaf5 lost the `/32`.
- **The failure was hidden by the old leaf.** Traffic kept flowing after
  leaf5 fell back to the `/24` route. Of leaves 1–4, only leaf2 (the old leaf)
  still had a usable ARP entry for the robot (its own kernel-learned one), so
  the controller's flow must have been reaching leaf2, which then bridged it
  over VXLAN to leaf1. That's inferred from the ARP-table events, not
  captured. It's also temporary: leaf2's entry eventually went `FAILED`,
  because its reachability probes reach the robot but the replies go to the
  anycast gateway MAC on the robot's own leaf.
- **Moves to leaf2 kept or recovered the IP.** In move 1, leaf2 learned it
  ~60 ms after leaf5 fell back to the `/24` route, consistent with leaf2
  resolving the robot itself once the controller's flow reached it. In move
  3, leaf2 still had its own entry from move 1, which zebra reactivated (the
  case zebra handles), so leaf5 had the `/32` back at +363 ms.
- **Sequence numbers rose on every move** (1, 2, 3, 4), as expected when the
  new leaf still has the robot as remote when it learns it. Duplicate
  detection didn't trip (4 moves, under the default 5-in-180-s limit).

### Same test with re-ARPs (`MOVE_OUTAGE_REARP_COUNT=29`)

Same settings, but after each move the robot re-sends its ARP 29 more times,
~100 ms apart (results in `~/packet_captures/bbm/move_outage_rearp_29`).

| Move | Outage (fabric part) | Instructions missed | New leaf's blocking entry removed | IP learned | leaf5 `/32` via new leaf |
|---|---|---|---|---|---|
| 1 leaf1 → leaf2 | 161 ms (164) | 2 | +307 ms | +371 ms | +597 ms |
| 2 leaf2 → leaf1 | 256 ms (205) | 3 | +425 ms | +503 ms | +866 ms |
| 3 leaf1 → leaf2 | 230 ms (176) | 2 | +391 ms | +452 ms | +695 ms |
| 4 leaf2 → leaf1 | 234 ms (181) | 2 | +422 ms | +503 ms | +722 ms |

- **Every move learned the IP, in both directions,** and leaf5 had the `/32`
  back 0.6–0.9 s after each move. All four moves were counted.
- **The robot's first ARP was still ignored every time.** The new leaf held
  zebra's remote `NOARP` entry until the old leaf's withdraw removed it
  (+307–425 ms); the IP was learned 61–80 ms later, i.e. from the next re-ARP
  (or, in move 1, the leaf resolving the robot itself). So the re-ARPs don't
  stop the race; they make sure an ARP arrives after it's over.
- **Without re-ARPs, moves to leaf2 only "worked" because moves to leaf1
  failed.** Here every move advertises MAC+IP, so every new leaf holds zebra's
  copy of the robot's IP (e.g. leaf2's own entry was replaced at +694 ms in
  move 2), and every first ARP is blocked. In the run without re-ARPs, leaf1
  never advertised MAC+IP, so leaf2 kept its own entry and zebra could
  reactivate it.
- **The per-move outage is unchanged:** 161–256 ms, 2–3 instructions missed,
  against 152–276 ms and 1–2 without re-ARPs. It still ends when the old leaf
  learns, through EVPN, that the MAC has moved (+193–301 ms); traffic then
  goes through the old leaf until leaf5's `/32` moves. Re-ARPs fix the lost
  IP, not the outage.
- **Side effect:** the re-ARPs are broadcast to every leaf, so leaf3 and leaf4
  briefly learn the robot's IP themselves on every move (kernel `STALE`,
  then replaced by zebra's remote copy). zebra keeps those entries inactive
  because the MAC isn't local there, but it still raises their local sequence
  number (leaf3/leaf4 show local 2–5 against remote 1–4). Nothing is
  advertised from them.

## Make-before-break (`stress_mbb`)

One run, same settings as `stress_separate` except `MV_MODE=mbb` and
`PING_INT=0.02` (pings every 20 ms instead of 200 ms). Results in
`~/packet_captures/mbb/stress_mbb`.

| | `stress_separate` (bbm) | `stress_mbb` (mbb) |
|---|---|---|
| Moves converged | 34 / 48 | 25 / 48 |
| New leaf never learned the IP | 14 / 48 | **24 / 48** |
| Missed, when the new leaf held a remote entry at move start | 14 / 37 | **24 / 28** |
| Missed, when it didn't | 0 / 11 | 0 / 20 |
| Robots whose sequence number rose on every move | 1 / 8 | **8 / 8** (all end at 6) |
| Longest silence on moves that learned the IP (median / max) | 0.30 / 2.23 s | **0.17 / 0.30 s** |
| Longest silence on moves that missed it (median) | 9.9 s | 7.9 s |
| BGP UPDATEs per move | 54.2 | 58.8 |

**Sequence numbers rise on every move, as predicted.** The new leaf still
holds the robot's MAC as remote when it learns it locally, so zebra advertises
"remote + 1" every time (`zebra/zebra_evpn_mac.c:2284`). All leaves agreed at
the end (`sequence_mismatches` = 0).

**The IP-learning failure is close to deterministic.** When the new leaf held
a remote `NOARP` entry for the robot, 24 of 28 moves missed the IP (86%,
against 38% in break-before-make). The robot's ARP goes out right after it
joins the new leaf (~72 ms), and the old leaf hasn't withdrawn yet (its port
only goes down at ~148 ms), so the entry is always still in the way. The 4
exceptions learned the IP 45–85 ms after their entry was removed (+0.29–0.40
s), from a later ARP, not by winning the race. With no remote entry, all 20
moves learned it.

**Moves alternate.** Every robot shows the pattern missed, learned, missed,
learned…: after a miss the next move learned the IP 20/20 times (the miss
clears the robot's entries everywhere), and after a learned move the next one
missed 18/20 times (the learned move reinstalls them).

**The per-move outage got shorter but didn't go away.** On moves that learned
the IP, the longest silence was 165 ms (median, max 300 ms), against 152–276 ms
in the move-outage test; two moves had only ~40 ms. The robot stays reachable
on the old link until that link goes down (~148 ms), but EVPN takes ~130–230
ms to tell the old leaf (move-outage test), so traffic sent to the old leaf in
between is still lost. A longer overlap should shrink this further; the stress
test has no setting for the overlap yet (it's fixed by the time the link
commands take).

**Note on ping loss at 20 ms pings:** loss reads ~14% even on moves that
worked, while their longest silence is only ~1.6% of the window. The test
counts expected pings as window ÷ `PING_INT`, so if `ping -i 0.02` sends
slower than every 20 ms the shortfall shows up as loss. Use longest silence,
not ping loss, for this run.

**How this test's make-before-break differs from real roaming:** the old
leaf's port goes down ~76 ms after the new link comes up, which also starts
the old leaf's withdraw. With an access point that stays up, the old leaf
would only withdraw once the new leaf's route arrived.

### Make-before-break in the move-outage test (0.5 s overlap)

`test_evpn_capstone_move_outage.py` with `MOVE_OUTAGE_MODE=mbb`,
`MOVE_OUTAGE_MBB_OVERLAP=0.5`, default 30 re-ARPs, everything else as before
(realistic roaming: both access ports stay up). One run of 4 moves, in
`~/packet_captures/mbb/move_outage_mbb_0.5s`. The robot joined the new link
at +40–68 ms and left the old one at +570–601 ms (overlap 520–532 ms).

| Move | Outage | Instructions missed | Old leaf took the MAC back during the overlap? | Final sequence |
|---|---|---|---|---|
| 1 leaf1 → leaf2 | **154 ms** (+552 to +706) | 1 | **yes**, +373 ms | 3 |
| 2 leaf2 → leaf1 | **0** | 0 | no | 4 |
| 3 leaf1 → leaf2 | 18 ms (+374 to +392) | 1 | **yes**, +393 ms | 7 (duplicate flagged) |
| 4 leaf2 → leaf1 | **0**\* | 0 | no | 8 (duplicate flagged) |

\*One probe failed at +14.995 s, at the very end of the window, so the test
marks move 4 "never recovered"; that's an artefact of the window edge, not an
outage.

**The overlap removes the per-move outage when nothing goes wrong.** Moves 2
and 4 lost no instructions: the robot was still on the old link while EVPN
moved its MAC (old leaf updated at +222–224 ms, well inside the 0.5 s
overlap). Compare 152–276 ms per move with break-before-make.

**But being on both links makes the old leaf take the robot back.** In moves 1
and 3 (leaf1 → leaf2), the sequence in move 1 was:

1. +40 ms: leaf2 learns the robot's MAC; +184 ms: leaf1 points it at leaf2.
2. +306 ms: leaf1's MAC+IP withdraw reaches leaf2 and clears its remote IP
   entry; +352 ms: leaf5 withdraws the `/32` and falls back to the subnet
   route.
3. +364 ms: leaf2 learns the robot's IP by ARPing for it. That ARP is a
   broadcast in VNI 1000, so it also reaches leaf1, which floods it out its
   access port, where the robot's old link is still on.
4. **+373 ms: the robot answers on its old link too, so leaf1 learns its MAC
   back on its port** and advertises it with a higher sequence number.
5. +508 ms: leaf2 hears that and hands the robot back to leaf1; +534 ms: the
   robot's next frame on the new link makes leaf2 take it again (another
   higher sequence number).
6. +552–570 ms: the old link goes off **while leaf1 still thinks the robot is
   on its port**, so traffic through leaf1 is dropped, until leaf2's newer
   route reaches leaf1 at +702 ms. Outage 154 ms.

Move 3 shows the same flip-flop (leaf1 at +393 ms, leaf2 at +548 ms); its
outage was only 18 ms, around leaf5's `/32` withdrawal, but the flip still
happened. Moves 2 and 4 (leaf2 → leaf1) didn't flip: there, leaf2 still had
its own ARP entry for the robot, so no leaf had to ARP for it (consistent with
the controller's traffic reaching leaf2 under the subnet route; inferred, not
captured).

**Consequences of the flip-flop:**
- Two extra sequence bumps per flip (final 3, 4, 7, 8 instead of 1–4).
- Extra MAC moves count towards duplicate detection, which uses FRR's
  defaults in this test (5 moves in 180 s): the robot was flagged duplicate
  on leaf1 and leaf2 from move 3. By default that's only a warning, so
  traffic kept flowing.
- An outage when the old link drops while the old leaf has just taken the
  robot back.

**IP learning:** with re-ARPs on, every move learned the IP (+364 to +736
ms) and leaf5 had the `/32` back at +939–1104 ms.

### Make-before-break, 10 moves with duplicate detection off

Same test and settings as the previous section (`MOVE_OUTAGE_MODE=mbb`,
0.5 s overlap, 30 re-ARPs, one robot, leaf1 ↔ leaf2), plus
`MOVE_OUTAGE_DAD=off` and `MOVE_OUTAGE_MOVES=10`. One run, in
`~/packet_captures/mbb/move_outage_mbb_dad_off_10mv`. Baseline: 0 of 78
instructions missed. The robot joined the new link at +15–93 ms and left the
old one at +547–640 ms (overlap 509–565 ms).

| Moves | Outage | Instructions missed | Old leaf took the MAC back? | Sequence bumps per move |
|---|---|---|---|---|
| 1, 3, 5, 7, 9 (leaf1 → leaf2) | **145, 143, 119, 104, 173 ms** | 1, 1, 1, 2, 1 | **yes, every time** (+372 to +465 ms) | 3 |
| 2, 4, 6, 8, 10 (leaf2 → leaf1) | **0** | 0 | never | 1 |

Move 8 is marked "never recovered" by the test because of one failed probe at
+14.997 s, at the very end of its window. That's the same window-edge
artefact as move 4 of the previous run, not an outage. The test now waits
100 ms after each window before judging its probes, so later runs shouldn't
show this.

**The direction decides it, 10 out of 10.** The earlier 4-move run showed the
same split; this run shows it every time over 5 moves each way. Every
leaf1 → leaf2 move flip-flops and is over the 100 ms budget. No
leaf2 → leaf1 move does.

**Duplicate detection off works as intended.** The robot's MAC moved 20 times
in about 150 s: local sequence after each move was 3, 4, 7, 8, 11, 12, 15, 16,
19, 20. That's +3 per flip-flop move (new leaf, old leaf back, new leaf again)
and +1 per clean move. No leaf flagged it as a duplicate (`isDuplicate`
false in every snapshot). With the defaults (5 moves in 180 s,
`bgpd/bgp_evpn_private.h:377-378`) the 5th move comes during move 3,
as in the previous run, where it was flagged from move 3.

**Who sends the ARP that the old link answers.** In every flip-flop move,
leaf2 (the new leaf) learns the robot's IP as `REACHABLE` 4–29 ms after leaf5
withdraws the robot's `/32` (+355–435 ms). `REACHABLE` means leaf2 sent an
ARP itself and got a reply. An entry made from someone else's ARP shows as
`STALE`, as in the clean moves. leaf1 relearns the MAC on its own port within
12 ms of that: +380, +465, +393, +389 and +372 ms. So leaf2 ARPs for the
robot as soon as traffic for it arrives and it has no entry. Its remote entry
was removed at about +300 ms by leaf1's MAC+IP withdraw. That ARP is
flooded in VNI 1000, so it also comes out of leaf1's access port. There the
robot is still on, and it answers.

**Why only leaf1 → leaf2.** After leaf5 drops the `/32`, the controller's
traffic follows the `45.0.0.0/24` subnet route. It apparently always lands on
leaf2:
- In leaf1 → leaf2 moves leaf2 is the new leaf. It has just lost its remote
  entry and has no local one yet, so it ARPs.
- In leaf2 → leaf1 moves leaf2 is the old leaf. It still has its own ARP
  entry for the robot, replaced only at +830–887 ms, and forwards to leaf1
  over VXLAN, so nobody ARPs and there's no outage.
This is inferred from which leaf ARPs. The subnet route's next-hop group
(`nhid 28` on leaf5) isn't recorded in the logs, so the leaf it hashes to
hasn't been checked directly.

**After the flip, leaf5 routes to the old leaf for a while.** In every
flip-flop move leaf5 installs the `/32` via **leaf1** (+676 to +790 ms),
withdraws it 85–137 ms later, and only then routes it via leaf2 (+967 to
+1109 ms). That's leaf1's MAC+IP route from the flip, superseded once leaf2
relearns the IP.

**The test adds part of this: re-ARPs start only after the overlap.** In the
test's robot agent (`move_outage_traffic.py`, `run_agent`), mbb sends one ARP
when it joins the new link, waits out the overlap, drops the old link, and
only then starts the re-ARPs. The first ARP is always ignored by the new
leaf, because it still holds zebra's `NOARP` copy of the robot's IP (problem
1). So from about +300 ms, when that copy is removed, to the first re-ARP,
the new leaf has no entry for the robot's IP. The first re-ARP comes at
+701–749 ms in the clean moves, 140–154 ms after the old link went off. That
gap is when leaf2 has to ARP. A robot that kept re-ARPing during the overlap
might close it before traffic reaches leaf2. That's a prediction; it hasn't
been run.

**Side effect of the flooded re-ARPs:** leaf3 and leaf4 briefly learn the
robot's IP as a local `STALE` entry from the robot's flooded ARP, which asks
for the anycast gateway that every leaf owns. zebra replaces it with its
remote copy about 100 ms later. No effect on traffic was seen.

### Repeat of the 10-move run, with the window-edge fix

Same settings as the previous section (`mode: mbb`, 0.5 s overlap,
30 re-ARPs, DAD off, 10 moves, one robot, leaf1 ↔ leaf2, per `settings` in
`results.json`). It is the first run with the fix that waits one budget
after each window before judging its probes (step 36). The robot agent
(`move_outage_traffic.py`) is unchanged since the previous run, so re-ARPs
still start only after the overlap. The test file was also edited before
this run. It's untracked, so what else changed can't be diffed. One run, in
`~/packet_captures/mbb/move_outage`. Baseline: 0 of 74 instructions missed.
The robot joined the new link at +36–91 ms and left the old one at
+554–661 ms (overlap 518–589 ms).

| Moves | Outage | Instructions missed | Old leaf relearned the MAC on its port | Sequence bumps per move |
|---|---|---|---|---|
| 1, 3, 5, 7, 9 (leaf1 → leaf2) | **117, 91, 150, 110, 184 ms** | 1 each | **yes, every time** (+423 to +439 ms) | 3 |
| 2, 4, 6, 8, 10 (leaf2 → leaf1) | **0** | 0 | never | 1 |

- **The previous run is reproduced.** The direction split held for 10 out
  of 10 moves again, with the same mechanism. leaf2 resolves the robot's IP
  (`REACHABLE`, +424 to +439 ms) right after leaf5 withdraws the `/32`
  (+399 to +431 ms). leaf1 relearns the MAC on its own port within 1.5 ms of
  that, in either order (moves 5 and 7 log leaf1 first). That fits one ARP
  exchange in which the robot answers on both links. leaf2 hands the robot
  back (+544 to +586 ms), then takes it again. The old link goes off while
  leaf1 still holds it, and the outage starts within 5 ms of that.
- **Outages, both runs together:** 91–184 ms on the ten leaf1 → leaf2 moves
  (median 131 ms). 9 of 10 were over the 100 ms budget, and each move missed
  1 instruction, 2 in one move. This run: 91–184 ms; previous: 104–173 ms.
- **The window-edge fix works.** Every move was judged recovered. The
  earlier runs had one false "never recovered" each.
- **leaf5 still routes to the old leaf for a while after each flip:**
  `/32` via leaf1 at +719 to +795 ms, withdrawn 98–254 ms later, then via
  leaf2 at +1054 to +1280 ms.
- **Sequence numbers:** leaf2's local sequence after the leaf1 → leaf2 moves
  was 3, 7, 11, 15, 19 (+3 per flip-flop move), and +1 on each move back.
  No duplicate flags (DAD off).
- **The IP was learned on every move:** +424 to +439 ms on moves to leaf2,
  +678 to +844 ms on moves to leaf1.

### Robot silent on its old link during the overlap (`MOVE_OUTAGE_MBB_OLD_TX=off`)

Same settings as the two 10-move runs above (`mode: mbb`, 0.5 s overlap,
30 re-ARPs, DAD off, 10 moves, one robot, leaf1 ↔ leaf2), plus
`mbb_old_tx: off`: during the overlap the robot sends nothing on its old
link but still receives there (step 40). One run, in
`~/packet_captures/mbb/move_outage_oldtx_off`. Baseline: 0 of 80
instructions missed. Overlap 509–570 ms.

| Moves | Flip-flop | Sequence per move | Outage | When |
|---|---|---|---|---|
| 1, 3, 5, 7, 9 (leaf1 → leaf2) | **none** | **+1** (leaf2 local 1, 3, 5, 7, 9) | 9, 33, 17, 23, 37 ms | at the start of the move only |
| 2, 4, 6, 8, 10 (leaf2 → leaf1) | none | +1 | 13, 38, 24, 36, 68 ms | at the start of the move only |

- **The flip-flop is gone.** Sequence numbers rose by exactly 1 per move,
  against +3 on every leaf1 → leaf2 move in the two runs with the old link
  open (10 of 10 moves). That confirms the robot's replies on its old link
  as the cause. It matches lostone's bond-based MBB, which never
  flip-flopped ([teammate_repos_mbb.md](teammate_repos_mbb.md)).
- **No outage when the old link goes off.** The 91–184 ms outages of the
  leaf1 → leaf2 moves are gone. On every move the only failed probes fall
  in one window at the start of the move.
- **That start-of-move outage was caused by the test.** It began right
  after the switch started (+16 to +45 ms) and ended at the robot's
  `t_on`, within about 10 ms (move 7: 20 ms). The agent dropped the old
  link's egress in the first `tc` call, but the robot's default route still
  pointed out the old link until the `ip` call finished, so its replies
  were dropped for that long (9–68 ms; 2 instructions missed in total, none
  over budget). A bond switches transmit in one step. The agent now drops
  the old link's egress only after the IP, route and first ARP have moved
  (step 41). This run's outage figures shouldn't be used as the fabric's.
- **The IP was learned on every move**, with no flip to slow it down. On
  moves to leaf2: IP at +306 to +518 ms, leaf5 `/32` via leaf2 at +474 to
  +704 ms (+1054 to +1280 ms in the previous run). On moves to leaf1: IP at
  +657 to +780 ms, `/32` at +853 to +1008 ms (about the same as before).
- No sign of leaf1's own entry failing early. On move 1, leaf1 swapped its
  local entry for zebra's remote copy at +422 ms, as in the earlier runs.
- **This run's raw files are gone.** The rerun below was saved into the same
  folder, and the earlier top-level files are no longer there. The figures
  above survive only in this doc and step 40.

### Robot silent on its old link, with the command order fixed

Same settings again (`mbb`, 0.5 s overlap, 30 re-ARPs, DAD off, 10 moves,
`mbb_old_tx: off`), run after the agent fix in step 41. Now the robot's IP,
default route and first ARP move to the new link before the old link stops
sending. One run, saved in
`~/packet_captures/mbb/move_outage_oldtx_off/bgp_evpn_capstone.test_evpn_capstone_move_outage/`.
Baseline: 0 of 74 instructions missed. The robot joined the new link at
+39 to +97 ms and left the old one at +586 to +662 ms (overlap 511–584 ms).

| Moves | Outage | Probes failed | Instructions missed | Flip-flop | Sequence |
|---|---|---|---|---|---|
| 1, 3, 5, 7, 9 (leaf1 → leaf2) | **0, 7, 0, 0, 0 ms** | 0, 1, 0, 0, 0 | 0 | none | +1 |
| 2, 4, 6, 8, 10 (leaf2 → leaf1) | **0** | 0 | 0 | none | +1 |

- **Make-before-break worked with no outage on 9 of 10 moves,** in both
  directions. Across the 10 moves, 1 of about 14,200 probes failed and no
  instruction was missed. The new leaf's local sequence was 1, 2, …, 10
  (+1 per move), and no leaf flagged a duplicate. Only the new leaf ever
  learned the robot's MAC on its port.
- **The start-of-move outage of the previous run is gone,** which confirms
  it came from the agent's command order.
- **The one failed probe (move 3, +428 ms, 7 ms) came from the fabric.**
  leaf5 withdrew the robot's `/32` at +424.0 ms and fell back to the subnet
  route. The controller's traffic then reached leaf2, whose remote entry for
  the robot had been removed at +362.9 ms (the old leaf's MAC+IP withdraw).
  leaf2 had to ARP for the robot (`REACHABLE` at +435.5 ms), and one probe
  was lost in that gap. This is the IP-learning problem's knock-on: the
  robot's first ARP was ignored while zebra's `NOARP` entry was in place.
  On every leaf1 → leaf2 move leaf2 resolved the robot 5.5–16.6 ms after the
  `/32` was withdrawn; only move 3 lost a probe in that window.
- **On moves to leaf1, the IP came from a later ARP:** 300–351 ms after
  leaf5 withdrew the `/32` (+709 to +775 ms), with no loss. As in earlier
  runs, the controller's traffic presumably kept going through leaf2, which
  still had its own entry (inferred; leaf5's subnet-route next hop isn't
  recorded).
- **leaf5 had the `/32` back via the new leaf** at +589 to +661 ms on moves
  to leaf2 and +895 to +987 ms on moves to leaf1.
- One run of 10 moves with one robot. Repeats, an overlap sweep and more
  robots are still to do.

### Robot silent on its old link, 50 moves

Same settings as the previous section, with 50 moves instead of 10
(`results.json` `settings`: `mbb`, 0.5 s overlap, `mbb_old_tx: off`,
30 re-ARPs at 0.1 s, DAD off, 15 s gap, one robot, leaf1 ↔ leaf2). The test
and agent are unchanged since that run. One run, in
`~/packet_captures/mbb/move_outage_50mv`. Baseline: 0 of 76 instructions
missed. The robot joined the new link at +18 to +131 ms and left the old one
at +546 to +710 ms (overlap 511–657 ms).

| Moves | Moves with loss | Outage on those moves | Probes failed | Instructions missed | Flip-flop | Sequence |
|---|---|---|---|---|---|---|
| 25 × leaf1 → leaf2 | 5 (moves 5, 17, 31, 33, 43) | 9.3, 2.0, 10.1, 8.6, 7.9 ms | 1 each | 0 | none | +1 |
| 25 × leaf2 → leaf1 | 1 (move 18, see below) | 493.9 ms as computed; a test artefact | 1 | 0 | none | +1 |

- **The no-outage result holds over 50 moves.** 6 of 71,752 probes failed
  and 0 of 7,177 instructions were missed. After every move the new leaf's
  local sequence equalled the move number (1 … 50) and every other leaf's
  remote sequence matched. No leaf flagged a duplicate.
- **The leaf1 → leaf2 losses are the same knock-on as move 3 of the previous
  run.** Each lost exactly one probe, sent 2.4–46 ms after leaf5 withdrew the
  `/32` and answered again just before leaf2 learned the robot's IP as
  `REACHABLE` (leaf2 ARPed for it itself). leaf2 learned it as `REACHABLE`
  on all 25 leaf1 → leaf2 moves, 4.0–55.6 ms after the withdrawal. So the
  window opens on every such move, and a probe was lost in it on 5 of 25
  (20%). How long the window lasted didn't predict the loss: losses came at
  13.4–55.6 ms, and moves with 41.7 and 48.2 ms lost nothing.
- **Move 18's 494 ms is not a fabric outage.** The one failed probe was sent
  at +4,071.8 ms, and the next probe the controller sent was at
  +4,565.7 ms. The test counts the outage from failed probe to next good
  probe (`_analyze`, `test_evpn_capstone_move_outage.py:1105-1131`). By
  then the fabric had settled: leaf5 had the `/32` via leaf1 at +1,062.6 ms,
  and no kernel event about the robot was logged between +1,062.6 and
  +6,434.7 ms. The move window holds 1,377 probes against 1,396–1,468 in the
  other moves, so about 60 probes (≈0.5 s at 10 ms) were never sent.
  The stream sender skips missed slots instead of catching up
  (`move_outage_traffic.py:76-79`). So the controller's sender stalled for
  about 0.5 s. The failed probe was probably answered but read late, after
  that stall. That's inferred; per-probe raw data isn't saved, so it can't
  be checked.
- **The direction asymmetry is unchanged.** leaf2 → leaf1 moves learned
  the IP 214–407 ms after the withdrawal, as `STALE` from a flooded ARP,
  with no fabric loss. As in earlier runs, the controller's traffic
  presumably kept going through leaf2, which still had its own entry
  (inferred, not captured).
- One run, one robot, 15 s gap. The gap and robot count are still untested
  at this setting.

### Robot silent on its old link, 50 moves at a 5 s gap

Same as the previous section, but with `MOVE_OUTAGE_GAP=5` and
`MOVE_OUTAGE_REPLAY_SLOWDOWN=1` (no slow-motion replay, which would not
fit in 5 s). `results.json` `settings`: `gap_s: 5.0`, everything else
unchanged. One run, in `~/packet_captures/mbb/move_outage_50mv_5s`.
Baseline: 0 of 79 instructions missed. Moves started 5.11–5.23 s apart
(mean 5.15 s). The robot joined the new link at +15 to +109 ms and left the
old one at +528 to +648 ms (overlap 508–572 ms).

| Moves | Moves with loss | Outage on those moves | Probes failed | Instructions missed | Flip-flop | Sequence |
|---|---|---|---|---|---|---|
| 25 × leaf1 → leaf2 | 4 (moves 15, 25, 27, 29) | 11.0, 11.7, 7.2, 11.3 ms | 1 each | 0 | none | +1 |
| 25 × leaf2 → leaf1 | 0 | – | 0 | 0 | none | +1 |

- **Moving three times as often changed nothing measurable.** 4 of 23,865
  probes failed and 0 of 2,384 instructions were missed. After every move
  the new leaf's local sequence equalled the move number (1 … 50), every
  other leaf's remote sequence matched, and no leaf flagged a duplicate.
- **The losses are the same knock-on as at 15 s:** 4 of 25 leaf1 → leaf2
  moves (16%), against 5 of 25 (20%) at 15 s. In each one, the lost probe
  was sent after leaf5 withdrew the `/32`, and probes got through again just
  before leaf2 learned the robot's IP.
- **No sender stall like move 18 of the 15 s run.** No failed probe falls
  outside the withdrawal-to-IP-learned window.
- **One slow handover with no loss (move 2, leaf2 → leaf1).** The MAC moved
  as usual (all leaves pointed it at leaf1 by +176 ms). But at +340 ms leaf5
  re-installed the `/32` via **leaf2**, the old leaf, with a new next-hop
  ID. The old leaf's IP entries were removed only at +3,262 ms, and leaf5
  withdrew the `/32` at +3,313 ms and had it via leaf1 at +3,419 ms. The
  test's EVPN snapshot at about +2 s confirms the in-between state: the MAC
  was local on leaf1 with sequence 2, and leaf5's `/32` pointed at
  10.0.0.2 ("Last update 00:00:02 ago"). Traffic kept flowing, presumably
  leaf5 → leaf2 → VXLAN → leaf1, since leaf2's MAC entry pointed at leaf1.
  Every other leaf2 → leaf1 move had the withdrawal at +302 to +526 ms. No
  move in the earlier runs (10, 10, 10 and 50 moves) had it later than
  1 s. Why leaf2 kept advertising its MAC+IP route for about 3 s isn't
  known: the leaves' FRR logs only show the snapshot's vtysh commands (no
  BGP or zebra debugs were on).
- One run, one robot. More robots, and gaps short enough for moves to
  overlap, are still untested. The re-ARPs start only after the old link
  goes off (`move_outage_traffic.py`, `run_agent`), so they end about 3 s
  after that: +3.53 to +3.65 s in this run and +3.55 to +3.71 s at 15 s.
  Below about 4 s, a move starts while the previous move's re-ARPs are
  still running. The test can't go below about 2 s plus the EVPN snapshot
  time with one robot either, because `_run_move` always sleeps 2 s
  (`REPLAY_AFTER`) and then takes the snapshot before the next move can
  start.

## Second problem: the per-move outage

Separate from the IP-learning failure, every move in the move-outage test cost
more than the 100 ms budget, whether or not the IP was learned. Counted moves
from both runs:

| Run, move | Robot off the air | New leaf learns MAC → old leaf updated | Traffic back at | Outage | Instructions missed |
|---|---|---|---|---|---|
| no re-ARP, 1 | 34 ms | 132 ms | +167 ms | 152 ms | 1 |
| no re-ARP, 2 | 84 ms | 227 ms | +332 ms | 276 ms | 2 |
| no re-ARP, 4 | 78 ms | 141 ms | +220 ms | 199 ms | 2 |
| re-ARP 29, 1 | 35 ms | 157 ms | +199 ms | 161 ms | 2 |
| re-ARP 29, 2 | 75 ms | 187 ms | +280 ms | 256 ms | 3 |
| re-ARP 29, 3 | 72 ms | 173 ms | +248 ms | 230 ms | 2 |
| re-ARP 29, 4 | 121 ms | 171 ms | +302 ms | 234 ms | 2 |

("Traffic back at" and the old leaf's update are both measured from the radio
switch. Move 3 of the no-re-ARP run isn't counted: leaf5 had already lost the
`/32`.)

**What happens:**
1. The robot goes off the air (22–121 ms here, while it switches).
2. The new leaf learns the robot's MAC from its first frame and advertises it
   with a higher mobility sequence number.
3. Meanwhile leaf5 still routes the controller's traffic to the **old** leaf,
   which still has the robot's MAC on its access port (the port stays up), so
   the traffic is dropped there.
4. The old leaf receives the new route (132–227 ms after step 2), points the
   robot's MAC at the new leaf, and forwards the traffic on over VXLAN.
   **Traffic came back within 0–18 ms of this step on every counted move.**
5. leaf5's `/32` moves to the new leaf later (0.6–0.9 s after the move, with
   re-ARPs), which changes the path but doesn't end the outage.

**What it means:**
- The outage is EVPN's normal convergence time for a MAC move in this fabric:
  one MAC route from the new leaf, through both spines, to the old leaf.
  It isn't caused by the IP-learning bug, and re-ARPs don't shorten it
  (152–276 ms without, 161–256 ms with).
- It exceeds a 100 ms instruction budget on every move. (The 100 ms
  instruction rate and budget come from the move-outage and warehouse tests,
  not from `project_proposal.md`, which doesn't give an instruction rate.)
- **Relevance to make-before-break:** if the robot stays reachable on the old
  access point until the old leaf has the update, the controller's traffic
  can still be delivered through the old leaf during steps 2–4, so this
  outage could largely disappear. That's a prediction, not a measurement; it
  depends on how long the robot keeps the old link. The same move style is
  predicted to lose the IP-learning race whenever the new leaf holds a
  remote entry (see "What decides the race").
- The stress test can't measure this outage well: its pings are 200 ms apart
  by default, coarser than the outage. `PING_INT` can be lowered (minimum
  0.01 s).

## Was it caused by the test?

The failing code path (above) involves no test code. Each harness choice:

| Harness choice | Effect |
|---|---|
| Kernel monitors (`bridge`/`ip monitor`) | Listen to netlink only; can't change state |
| Visualizer | Only posts link changes to the visualizer server during moves; never contacts routers |
| Route sampler | Read-only, but may delay bgpd's processing (above); **no sampler-off run yet** |
| Router configs | Checked-in, identical apart from the DAD line (and leaf5's identity-swapped copy of leaf1 in shared mode) |
| `DAD=off` | Not on the IP-learning path; **not yet tested with DAD on** |
| Robot moves its IP to a fresh interface | Forces an ARP on every move, which *helps*; a real robot roaming on one NIC may not ARP at all (not tested) |
| Old leaf's port goes down (bbm) | *Helps*: starts the withdraw early, which a real warehouse wouldn't do |
| Robot reattaches in ~70–90 ms | Affects how often the race is won, not whether it can be lost |

**Why it may not have been reported:** in `stress_separate`, 71% of moves
converged, so a single-move test will usually pass. The team's original
single-move test (`test_evpn_capstone_mobility.py`) also waits up to 90 s for
leaf5's `/32` after its move (`count=90, wait=1`), which would give a failed
move time to recover. Same-subnet traffic or MAC-only checks never show it.

## Evidence from other teams' branches

Full review: [teammate_repos_review.md](teammate_repos_review.md); the
make-before-break part, compared with our MBB runs, is in
[teammate_repos_mbb.md](teammate_repos_mbb.md). Their
harnesses differ from ours, so these are supporting results, not part of
the stress-run evidence above.

- **Independent confirmation.** guna's single-robot break-before-make study
  (20 matched trials, `week5-ip-relearning-causal/`) found the same chain:
  zebra's `NOARP` entry, the ignored ARP, the withdraw, the lost `/32`. One
  gateway ARP right after the move recovered 1 of 5 runs, the same as no ARP.
  One ARP after 3 s, or four ARPs 250 ms apart, recovered 5 of 5.
- **A zebra patch exists, and it didn't fix this.** yifan, zv1 and lostone
  carry the same change: `zebra_evpn_rem_neigh_install()` no longer calls
  `dplane_rem_neigh_add()`. It isn't in upstream master. It leaves
  `zebra_evpn_neigh_uninstall()` (`zebra/zebra_evpn_neigh.c:801`) in place,
  and the kernel deletes an entry by address whoever created it (Linux v6.8,
  `net/core/neighbour.c:1941-1952`). In lostone's MBB runs, 6 of 96 moves
  missed the IP both without the patch (11-17) and with it (11-18). With the
  patch, the new leaf already had a kernel `STALE` entry from the robot's
  flooded gateway ARPs. The robot's ARP raised no netlink event, and the old
  leaf's withdraw deleted the entry.
- **Other harnesses hide the race.** lostone's BBM waits 50 ms between links
  (our `stress_delay_0.05s` setting, no misses). In its MBB, 84 of 96 moves
  had the robot's MAC move start ~285 ms before the deliberate ARP, from a
  frame that wasn't identified. Only first moves were blocked.
- **The anycast explanation needs checking.** guna traced two probes during
  a missing `/32` that went to a non-local leaf (leaf3). One was lost. The
  other was bridged to the robot's leaf and answered.
- **Make-before-break, other harnesses:** guna saw the new leaf's sequence
  reach 3 in 8 of 10 MBB-250/500 runs, and the `/32` missing in 9 MBB runs,
  with leaf5 resolving to leaf3. lostone saw 162 duplicate flags in each MBB
  run and none in 5 of its 6 BBM runs. Neither established the cause.
- **lostone's bond-based MBB never flip-flopped.** In both runs (96 moves
  each), lostone's FDB logs show the old leaf never relearned the robot's MAC
  during the overlap, and sequence numbers rose by 1 per move. Its robot
  transmits only on the new link. guna's robot keeps transmitting on the old
  link, and its sequence reached 3 more often as the overlap grew, as in our
  flip-flop. This points to the robot's old-link replies as the cause of our
  flip-flop (inference; not captured).

## Caveats

- **Seen with FRR on a Linux dataplane.** The failure depends on how the
  kernel treats zebra's `NOARP` entries and on zebra leaving remote entries in
  place, which zebra's own code marks as "TBD". Other platforms may handle ARP
  for remote hosts differently, so this is an FRR limitation in this setup
  rather than a flaw in the EVPN protocol.
- **Six runs, one each** (four of them `ATTACH_DELAY` controls). All are
  break-before-make, 8 robots, 10 s, with the sampler on. Run-to-run spread, move-rate effects, make-before-break and
  sampler-off behaviour are all unmeasured.
- **Moves are emulated.** A move is a veth link change in about 70–90 ms,
  with no real Wi-Fi roaming, on one VM shared by every node.
- **The robots are artificially regular.** Each sends one ping on attach and
  otherwise only answers the controller.

## Relation to the RFCs

- **RFC 7432 §15 (MAC Mobility, in the base EVPN standard):** MAC mobility sequence numbers only resolve competing
  routes for the *MAC*. Here the MAC converges; the IP is what's lost.
- **RFC 9721 §6.8** (Mobility Convergence, in Extended Mobility Procedures for
  EVPN-IRB; optional): when a leaf learns a host's MAC locally and
  "has a prior remote MAC-IP host route for this MAC address, an ARP/NDP probe
  MAY be triggered … to learn the MAC-IP address as a local adjacency". That
  is the case zebra leaves as "TBD". On Linux, zebra would also have to
  replace its `NOARP` entry first (netlink updates are admin,
  `net/core/neighbour.c:1959`), or the probe's reply would be ignored too.

## Next steps

1. **Repeat runs** of each setting, with the visualizer and sampler off, for
   spread and to rule the sampler out.
2. **Repeat the threshold delays:** 0.025 s missed once and 0.05 s didn't,
   with as little as 18 ms to spare. Repeat both a few times to get a miss
   rate rather than a single count, and under load (next item). Consider
   adding the per-move race margin as a column in `moves.csv`.
3. **Withdraw latency under load:** measure it with more robots (40, 100) or a
   shorter `MV_INT`, and add it as a per-move column in `moves.csv`.
4. **Make-before-break runs**, to test the prediction above.
5. **`DAD=on` run**, to rule it out.
6. **Manual reproduction on the original topology**
   (`--topology-only test_evpn_capstone_mobility.py`, move the robot by hand,
   check that the new leaf still shows `NOARP proto zebra` after the robot
   ARPs).
7. **Scale toward the small warehouse:** 40, then 100 robots on the same 4
   robot leaves.
8. **Confirm the anycast explanation** for lost traffic with a capture on a
   non-local leaf during a failed move, and the wasted UPDATEs with
   `debug bgp updates` on leaf5.
9. **Improvement path:** implement the RFC 9721 §6.8 probe (optional ARP probe from the
   new leaf to relearn a moved host's IP) in zebra's "TBD"
   branch and measure it with the stress test (`ip_not_learned` should drop
   to about 0 without any delay).

## Reproducing

From `~/frr/tests/topotests/bgp_evpn_capstone` inside the frr-dev container:

```bash
sudo -E NUM_ROBOTS=8 MV_COUNT=6 MV_INT=10 MV_MODE=bbm VIS=on ROUTE_SAMPLER=on python3 -m pytest -s test_evpn_capstone_mobility_stress.py
```

Add `CTRLER_SUBNET=shared` for `stress_shared`, or `ATTACH_DELAY=0.025`,
`0.05`, `0.3` or `0.5` for the delay runs. Results are in
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone_mobility_stress/stress/`:
`moves.csv` (per move), `events.csv` (raw timeline), `sequence.csv`,
`final_state.txt`, `route_samples.csv` and `summary.json`.

Move-outage test, make-before-break (from the same directory):

```bash
sudo -E MOVE_OUTAGE_MODE=mbb python3 -m pytest -s test_evpn_capstone_move_outage.py
```

Options: `MOVE_OUTAGE_MBB_OVERLAP` (default 0.5 s), `MOVE_OUTAGE_MBB_OLD_TX`
(`on` by default; `off` stops the robot sending on its old link during the
overlap, though it still receives there), `MOVE_OUTAGE_REARP_COUNT`
(default 30), `MOVE_OUTAGE_MOVES` (moves per robot, default 4),
`MOVE_OUTAGE_DAD=off` (adds `no dup-addr-detection` so more than 4 moves can
run), and `MOVE_OUTAGE_ROBOTS=N` (robots spread over leaf1 and leaf2, moves
staggered across `MOVE_OUTAGE_GAP`). Results are in
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone_move_outage/move_outage/`:
`results.json` and one timeline per move.
