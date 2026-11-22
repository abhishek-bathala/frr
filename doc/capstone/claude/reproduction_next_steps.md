# Next Steps: Reproducing the Mobility Problem

"Reproduce the problem" means showing, with measurements, that EVPN's existing
mobility handling breaks down as robot movement approaches warehouse levels.
Nothing is being fixed yet. The goal is to answer the problem statement's
question, "are existing mechanisms sufficient?", with evidence of where and
how they degrade.

Results so far: [mobility_reproduction_findings.md](mobility_reproduction_findings.md).

This plan is append-only. Steps are never removed or rewritten: when a
step's status changes it gets a **Status** line (done, dropped or superseded,
with the result or reason), so earlier ideas and approaches that didn't work
stay on record. Step numbers are fixed IDs, in the order the steps were added.

Steps are grouped by move style, in this order: **make-before-break** (the
current focus), **common** (applies to both, or to the problem in general),
then **break-before-make**. Within each group they are in chronological
order, **newest at the top**; a new step goes at the top of its group. The
original plan (steps 1–10) is the oldest part of the common group. The
reference sections it was written with (target scale, the DAD decision) are
at the bottom of the file.

## Steps

### Make-before-break (newest first)

43\. **Step 42's setting at a 5 s gap** **[Done; same as at 15 s, 0
instructions missed, 1-probe loss on 4 of 25 leaf1 → leaf2 moves]**. This
is the first of step 42's possible next runs: `MOVE_OUTAGE_GAP=5
MOVE_OUTAGE_REPLAY_SLOWDOWN=1`, everything else as in step 42. At 5 s each
move still settles before the next one starts. The question is whether
moving three times as often changes the loss rate.
**Status: done.** `~/packet_captures/mbb/move_outage_50mv_5s`:
- moves started 5.11–5.23 s apart; 4 of 23,865 probes failed and 0 of
  2,384 instructions were missed; +1 sequence per move, no duplicates, no
  flip-flop;
- 4 of 25 leaf1 → leaf2 moves lost one probe in the window between leaf5's
  withdrawal and leaf2 learning the IP (5 of 25 at 15 s);
- one unexplained slow handover (move 2): the old leaf kept advertising
  the robot's MAC+IP route for about 3 s, and leaf5 routed via the old leaf
  meanwhile, with no loss. It hasn't been seen in any other run, and the
  runs don't log enough to explain it.
Possible next runs, to be agreed first: `MOVE_OUTAGE_ROBOTS` > 1; a gap
below about 3.5 s, where moves overlap (the previous move's 3 s of re-ARPs
would still be running, and the agent doesn't stop them, so decide first
whether it should);
`MOVE_OUTAGE_REARP_COUNT=0`; BGP/zebra debugs on leaf1 and leaf2 to catch
the move-2 case if it comes back.
**Correction:** the "about 3.5 s" above is too low. The re-ARPs start only
after the old link goes off, so they end at about +3.5 to +3.7 s (from
`t_old_off` + 3 s in steps 42 and 43). Moves stay independent only at a gap
of about 4 s or more. With one robot the test can't go below about 2 s plus
the EVPN snapshot time either, because `_run_move` always sleeps 2 s
(`REPLAY_AFTER`) before the snapshot. For a move per second across the
fabric, use more robots (for example 5 robots at a 5 s gap).

42\. **Repeat step 41's setting with 50 moves** **[Done; no flip-flop, 0
instructions missed, 1-probe loss on 5 of 25 leaf1 → leaf2 moves]**. This is
the first of step 41's possible next runs ("repeat this setting for
spread"), with `MOVE_OUTAGE_MOVES=50` and everything else unchanged (mbb,
0.5 s overlap, `MBB_OLD_TX=off`, DAD off, 15 s gap, one robot). The aim is
to get a rate for the small loss seen once in 10 moves, before changing
the gap or the robot count.
**Status: done.** `~/packet_captures/mbb/move_outage_50mv`:
- 6 of 71,752 probes failed and 0 of 7,177 instructions were missed;
  +1 sequence per move (1 … 50), no duplicate flags, no flip-flop;
- 5 of 25 leaf1 → leaf2 moves lost one probe (2–10 ms) while leaf2 ARPed
  for the robot after leaf5 dropped the `/32`. This is the IP-learning
  knock-on;
- move 18 (leaf2 → leaf1) shows 494 ms, but that was the controller's
  sender stalling about 0.5 s at +4.07 s, long after the fabric settled.
  It's a test artefact and isn't counted as an outage.
Possible next runs, to be agreed first: the same setting at a 5 s gap
(`MOVE_OUTAGE_GAP=5 MOVE_OUTAGE_REPLAY_SLOWDOWN=1`), `MOVE_OUTAGE_ROBOTS`
> 1, and `MOVE_OUTAGE_REARP_COUNT=0` for the IP-learning problem alone.

41\. **Fix the order in which the robot's old link goes quiet** **[Done;
verified in a run]**. In the first `MOVE_OUTAGE_MBB_OLD_TX=off` run (step 40),
the agent dropped the old link's egress before moving the robot's IP and
default route. Until the route moved, the robot's replies still left by the
old link and were dropped, giving a 9–68 ms outage at the start of every
move (it ended at `t_on`, within about 10 ms). A bond switches transmit in
one step, so this came from the test. Now the old link's egress is dropped
right after the IP, route and first ARP have moved, and the overlap still
runs 0.5 s from the ARP. The `on` setting is unchanged. Checked offline
(compile and simulated `tc` order); not yet run. Next: rerun step 40's
settings. Expected: no outage in either direction and +1 sequence per
move, or else a remaining outage that belongs to the fabric.
**Status: done.** Rerun of step 40's settings
(`~/packet_captures/mbb/move_outage_oldtx_off/bgp_evpn_capstone.test_evpn_capstone_move_outage/`):
- no outage on 9 of 10 moves and no instruction missed; +1 sequence per
  move, no flip-flop, no duplicate flags;
- the start-of-move outage is gone;
- the one failed probe (move 3, 7 ms) came while leaf2 ARPed for the robot,
  11.5 ms after leaf5 dropped the `/32`. That's a knock-on of the
  IP-learning problem, not the test.
This rerun overwrote the first `oldtx_off` run's raw files; its figures
remain in the findings doc and step 40. Possible next runs, each to be
agreed first:
- repeat this setting for spread;
- overlap sweep (0.1, 0.25, 0.5 s) with the old link silent;
- `MOVE_OUTAGE_REARP_COUNT=0` with the old link silent, for the IP-learning
  problem alone;
- `MOVE_OUTAGE_ROBOTS` > 1.

40\. **Option to stop the robot sending on its old link during the overlap**
**[Done; flip-flop gone, no outage on 9 of 10 moves after step 41]** (`MOVE_OUTAGE_MBB_OLD_TX=off`, move-outage test,
mbb only). This tests the idea from steps 32 and 38. In every leaf1 → leaf2
move so far, the robot answered leaf2's flooded ARP out its old link, so
leaf1 took the MAC back (steps 35 and 39). With `off`, the robot's agent
(`move_outage_traffic.py`) drops everything the robot sends on the old link
from the start of the overlap, but the robot still receives there. That's
like lostone's active-backup bond, which transmits only on the new link and
never flip-flopped. The drop is added in the same `tc` call that turns the
new link on, before the IP moves and the first ARP goes out. At the end of
the overlap only the receive side is closed. The default `on` behaves as
before. The setting is saved as `mbb_old_tx` in `results.json` and appears
in the verdict line. Checked offline: both files compile, and a simulated
agent run gives the expected `tc` order in both settings, with no duplicate
or missing filters over four moves. Not yet run in the container.
Expected, if the old-link replies cause the flip-flop:
- the leaf1 → leaf2 moves stop flip-flopping;
- they lose their 91–184 ms outage and drop to +1 sequence per move, like
  the leaf2 → leaf1 moves.
To watch: leaf1 gets no replies to its own ARP probes for the robot during
the overlap, so its entry may fail sooner. That's realistic for a robot
that only transmits on its new link.
**Status:** first run done (`~/packet_captures/mbb/move_outage_oldtx_off`).
No move flip-flopped (+1 sequence per move, against +3 on every leaf1 →
leaf2 move before), and nothing was lost when the old link went off. That
confirms the robot's old-link replies as the cause. Every move had a 9–68 ms
outage at its start, which came from the agent's command order (step 41).
leaf1's entry didn't fail early. Rerun needed after step 41.

39\. **Repeat of the 10-move MBB run, with the window-edge fix** **[Done]**
(`~/packet_captures/mbb/move_outage`). Same settings as step 35, with the
step 36 fix; the robot agent is unchanged. Step 35's result reproduced
exactly. Every leaf1 → leaf2 move flip-flopped: the old leaf relearned the
MAC at +423 to +439 ms, the outage was 91–184 ms with 1 instruction missed,
and +3 sequence bumps. No leaf2 → leaf1 move did (0 outage, +1). All 10 moves
were judged recovered, so the fix works. The test file was edited before
this run (untracked, so the change can't be diffed). Across both runs: 10
of 10 leaf1 → leaf2 moves flip-flopped, outage median 131 ms, 9 of 10 over
the 100 ms budget. Next, as already proposed (step 38): stop the robot
answering on its old link, and re-ARP during the overlap, each to be agreed
first.
**Status: done.**

38\. **Collected the make-before-break parts of the branch review** **[Done (write-up); runs open]** into
[teammate_repos_mbb.md](teammate_repos_mbb.md), plus a new check of
lostone's MBB FDB logs. The old leaf never relearned the robot's MAC during
the overlap (0 of 96 moves in each run), and sequence numbers rose by 1 per
move. lostone's robot is a bond that transmits only on the new link. guna's
robot keeps transmitting on the old link, and it shows our +3 pattern (8 of
10 runs at 250 or 500 ms overlap). Both support step 32's idea of stopping
the robot answering on its old link. Proposed MBB runs, each to be agreed
first:
- no old-link replies during the overlap (bond-like active-backup,
  `arp_ignore`/`arp_filter`, or an egress drop);
- re-ARPs during the overlap;
- `MOVE_OUTAGE_REARP_COUNT=0`;
- leaf5 subnet-route next hop per move.
**Status: done (write-up).** The runs are open.

36\. **Fix the window-edge "never recovered" artefact in the move-outage
test.** **[Done; verified in a run]** A probe sent in the last few ms of a move's window had no reply yet
when the window was judged, so it counted as failed and a clean move was
marked "never recovered" (move 8 of `move_outage_mbb_dad_off_10mv`, move 4 of
`move_outage_mbb_0.5s`). The test now waits one `BUDGET` (100 ms) after the
window closes before reading the probes, so every probe in the window gets
its full budget. The window itself is unchanged; each move starts about
100 ms later than before. Checked by compiling only; not yet run.
**Status: done.**
**Status:** verified in its first run (step 39): all 10 moves were judged
recovered, with no false "never recovered".

35\. **Make-before-break, 10 moves, duplicate detection off** **[Analysed]**
(`~/packet_captures/mbb/move_outage_mbb_dad_off_10mv`). Every leaf1 → leaf2
move flip-flopped (104–173 ms outage, +3 sequence bumps). No leaf2 → leaf1
move did (0 outage, +1). The ARP the old link answers is sent by leaf2: it
needs the robot's IP as soon as traffic arrives after leaf5 drops the `/32`,
and the subnet route apparently always lands on leaf2. The gap exists partly
because the test's robot only starts re-ARPing after the overlap. Possible
next runs, each to be agreed first:
- re-ARP during the overlap (test agent change);
- record leaf5's subnet-route next hops, to confirm it lands on leaf2;
- stop the robot answering ARP on its old link during the overlap;
- fix the window-edge "never recovered" artefact (a probe sent in the last
  `BUDGET` of a window with no reply yet is counted as failed).
**Status: analysed.**

34\. **Move-outage test: duplicate address detection and robot-count options.** **[Partly done; robot option not run]**
The first two extensions from step 33, in
`test_evpn_capstone_move_outage.py`:
- `MOVE_OUTAGE_DAD=off` adds `no dup-addr-detection` to each leaf's config
  (the checked-in `frr.conf`, copied to the log directory), the same way as
  the stress test's `DAD=off`, so a robot can move more than 5 times in
  180 s (RFC 7432 §15.1, MAC Duplication Issue: a MAC that moves 5 times in
  180 s is flagged as a duplicate, FRR's default) without being flagged.
- `MOVE_OUTAGE_ROBOTS=N` (1–32, default 1). Robot *k* is `45.0.0.(99+k)`,
  MAC `00:00:00:00:01:(k−1)`, starts on leaf1 or leaf2 in turn (like the
  stress test spreads robots) and moves every `MOVE_OUTAGE_GAP` seconds;
  robots are staggered evenly across the gap. Each robot has its own probe
  stream, echo responder and roam agent; one set of kernel monitors per leaf
  is shared and split by robot. The visualizer's leaf colours follow robot1,
  and the slow-motion replay only runs with one robot. With one robot the
  output files are named as before; with more, timelines are
  `robotN_moveM_timeline.txt` and `results.json` has a `robot` field per move.
Checked by compiling and offline tests (config generation, per-robot event
parsing, the staggered schedule); not yet run in the container.
**Status: implemented, waiting for the first runs.** First run done (step 35): `MOVE_OUTAGE_DAD=off` worked (20 MAC moves, no duplicate flag). `MOVE_OUTAGE_ROBOTS` not run yet.

33\. **Decision: the move-outage test is the main tool for make-before-break;
the stress test is on hold.** **[In progress]** The outage test models make-before-break
realistically (both access ports stay up, so the old leaf only learns of the
move through EVPN), controls the overlap, and measures with 10 ms probes and
kernel timestamps. The stress test's `mbb` mode brings the old leaf's port
down ~76 ms after the new link comes up, which starts the withdraw early, and
its ping-loss figure is unreliable at small `PING_INT`. The stress test stays
available for scale later (many robots, four leaves); its runs stay as
background. Extensions to the outage test, in order:
- option to turn duplicate address detection off, so runs can have more than
  4 moves;
- option to add robots, distributed like the stress test;
- anything further only after agreeing it first (candidates: round-robin over
  leaf1–4, a per-move check of leaf5's `/32`, stopping the robot answering on
  its old link during the overlap).
**Status: in progress.** The first two extensions are implemented (step 34).

32\. **Make-before-break MAC flip-flop.** **[Open]** In the first make-before-break
move-outage run (0.5 s overlap, `~/packet_captures/mbb/move_outage_mbb_0.5s`),
moves without trouble had no outage at all (moves 2 and 4), but in moves 1
and 3 the robot answered a flooded ARP on its old link during the overlap, so
the old leaf took its MAC back, the new leaf took it again, and when the old
link dropped the old leaf still held it: a 154 ms outage in move 1, two extra
sequence bumps per flip, and duplicate detection tripped from move 3. Ideas to
test:
- stop the robot answering on the old link during the overlap (for example
  drop ARP on the old link's egress, or `arp_ignore`/`arp_filter` settings on
  the robot), and see if the flip-flop goes away;
- an overlap sweep (0.1, 0.25, 0.5, 1 s), to see how the outage and the flip
  rate depend on it;
- the same run with `MOVE_OUTAGE_REARP_COUNT=0`, for the IP-learning problem
  in make-before-break.
**Status: open.**

31\. **Make-before-break option in the move-outage test.** **[Built; first run done]** Added
`MOVE_OUTAGE_MODE=mbb` and `MOVE_OUTAGE_MBB_OVERLAP` (default 0.5 s) to the
`test_evpn_capstone_move_outage.py` and its robot agent
(`move_outage_traffic.py`). In mbb the robot's radio comes on at the new link,
its IP, default route and first ARP move there, and only after the overlap
does the radio go off on the old link; both access ports stay up, as in the
existing mode. The test records when the old link went off
(`t_old_off`, `mbb_overlap_ms`) and treats the whole outage as the fabric's.
The checks that require an outage (every move loses probes, recovery only
after the EVPN update, worst outage over budget) are skipped in mbb, since no
outage is a possible correct result; everything is still measured. Default
`bbm` behaves as before. This gives the realistic-roaming make-before-break
case that step 30 asked for (old leaf's port stays up). **Status: built,
checked offline (agent command order in both modes; both files compile); not
yet run in the container.** Next: run it with re-ARPs on (outage only) and
with `MOVE_OUTAGE_REARP_COUNT=0` (IP-learning problem), and sweep the overlap
(e.g. 0.1, 0.25, 0.5 s).
**Status:** first run done (0.5 s overlap, re-ARPs on); see step 32.

30\. **First make-before-break stress run** **[Done]** (`stress_mbb`, 8 robots,
`MV_MODE=mbb`, `PING_INT=0.02`). **Status: done.** Sequence numbers rose on
every move (8/8 robots). The IP was missed on 24/48 moves, 24 of the 28 with a
remote entry on the new leaf (near-deterministic, as predicted), with the same
missed/learned alternation. On moves that learned the IP the longest silence
was 165 ms (median), so the short overlap (~76 ms) didn't remove the outage.
Next:
- add an overlap setting to the stress test (how long the robot stays on both
  links before the old one drops) and sweep it, e.g. 0.1, 0.25, 0.5 s, to see
  whether a longer overlap removes the outage for moves that learn the IP;
- add a make-before-break mode where the old leaf's port stays up (only the
  robot's old link goes quiet), closer to real roaming;
- fix or replace the ping-loss metric at small `PING_INT` (it counts expected
  pings from `PING_INT`, so a slower send rate reads as loss).

29\. **Focus on make-before-break.** **[Done]** The user has been assigned to the
make-before-break team, so mbb is the focus until further notice. Checked:
every valid run so far is break-before-make (all six stress runs recorded
`MV_MODE=bbm`; the move-outage test turns the robot's radio off on the old
link before turning it on at the new one, with both access ports staying up).
Results are now split into `~/packet_captures/bbm/` (all existing runs,
including the two `move_outage*` runs referenced in step 28) and
`~/packet_captures/mbb/` (empty so far). Next:
- make-before-break stress runs with full parameters saved (`MV_MODE=mbb`,
  same settings as `stress_separate`), to test the prediction that the
  IP-learning race is lost whenever the new leaf holds a remote entry
  (step 22);
- measure the per-move outage for make-before-break, which is predicted to
  be much smaller because the robot stays reachable on the old link while
  EVPN converges. The stress test's default 200 ms pings are too coarse for
  this; lower `PING_INT`.
**Status: open.** The folder move itself needs `sudo` (the result folders
are root-owned).
**Status: done.** The folder split is in place: `~/packet_captures/bbm/`
holds the six stress runs and both `move_outage*` runs, and
`~/packet_captures/mbb/` holds the MBB runs. Both next items were done: the
make-before-break stress run (step 30, `stress_mbb`, full parameters in
`summary.json`) and the per-move outage under make-before-break, measured
with the move-outage test's 10 ms probes (steps 31, 32 and 35) rather than
the stress test's pings.

22\. **Make-before-break runs** **[Done]** (`MV_MODE=mbb`) with full parameters saved, to
test the prediction that the race is always lost when the new leaf holds a
remote entry. **Status: open.** Earlier mbb runs were disregarded.
**Status: done** (step 30, `stress_mbb`, parameters saved in `summary.json`).
The prediction mostly held: the IP was missed on 24 of the 28 moves where
the new leaf held a remote entry, and on none of the 20 where it didn't. The
4 exceptions learned it from a later ARP, after the entry was removed.

17\. **Manual reproduction on the original topology** **[Open]**
(`--topology-only test_evpn_capstone_mobility.py`, move the robot by hand
make-before-break, check that the new leaf still shows `NOARP proto zebra`
after the robot ARPs), to show the failure without the stress test.
**Status: open.**

### Common (newest first)

37\. **Reviewed five other teams' branches** **[Done (review); follow-ups open]** (guna, lostone, vansh, yifan,
zv1); details in [teammate_repos_review.md](teammate_repos_review.md).
Findings:
- Three branches carry the same zebra patch: remote host IPs are no longer
  installed in the kernel, so no `NOARP` entry exists. It isn't upstream.
- lostone has one MBB run with the patch and one without. Both missed the IP
  on the same 6 of 96 moves. With the patch, zebra's delete on the old
  leaf's withdraw removed the new leaf's kernel-learned entry instead (the
  "TBD" risk).
- guna confirmed the `NOARP` mechanism on its own. One immediate ARP didn't
  help; four ARPs 250 ms apart did.
- lostone's BBM waits 50 ms between links, and its MBB usually starts the
  MAC move ~285 ms before the robot's ARP. Both hide the race.
- guna traced one probe sent to a non-local leaf that was still delivered.
Possible next runs, each to be agreed first:
- measure the patch in the stress and move-outage tests (with and without);
- record leaf5's subnet-route next hop (guna saw it resolve to an
  uninvolved leaf);
- capture on a non-local leaf to check the anycast explanation;
- try lostone's bond-based make-before-break as a cross-check.
**Status: done (review).** The follow-ups are open.

28\. **Add the move-outage test.** **[Done; run twice]**
`test_evpn_capstone_move_outage.py` and `move_outage_traffic.py`, plus matching
`templates/index.html` and `visualizer_server.py` changes (new visualizer
events), added to the working tree. It
measures the outage on moves that work, with realistic roaming (robot-side
radio, both access ports stay up), 10 ms probes, kernel-timestamped events and
VXLAN data-plane learning off by default. It works around the IP-learning
problem by having the robot send 30 extra ARPs after each move. Plan: run it
as-is for the per-move outage; then, once a zebra fix for the IP-learning
problem (step 18) is built, run it with `MOVE_OUTAGE_REARP_COUNT=0` as a
realistic-roaming check of the fix. **Status: files copied (not committed); not yet run here.**
**Status:** run once with `MOVE_OUTAGE_REARP_COUNT=0` (4 moves, leaf1 ↔ leaf2,
standard zebra; results in `~/packet_captures/move_outage`). Every counted
move lost 1–2 instructions (130–250 ms of fabric outage, over the 100 ms
budget). Both leaf2 → leaf1 moves also lost the robot's IP (no `/32` on
leaf5), but traffic kept flowing through the old leaf, which still had its
own ARP entry for the robot. See the findings doc.
**Status:** run again with `MOVE_OUTAGE_REARP_COUNT=29`
(`~/packet_captures/move_outage_rearp_29`). Every move learned the IP (from a
re-ARP 61–80 ms after the old leaf's withdraw cleared the blocking entry) and
leaf5 had the `/32` back within 0.6–0.9 s. The per-move outage stayed at
161–256 ms (2–3 instructions missed), so re-ARPs fix the lost IP but not the
outage.

20\. **Visualizer and route sampler off.** **[Open]** All valid runs had both on. Repeat
the main settings with both off to rule out the sampler, which queries
bgpd on the thread that processes withdraws. **Status: open.**

18\. **Improvement path: implement RFC 9721 §6.8 probing in zebra** **[Open (Phase 5 candidate)]** (RFC 9721 is
Extended Mobility Procedures for EVPN-IRB; §6.8, Mobility Convergence,
describes an optional ARP probe from the new leaf to relearn a moved host's
IP). When a MAC
becomes local and a remote MAC+IP entry exists, replace the `NOARP` entry
and probe the host to confirm its IP (zebra's "TBD" branch in
`zebra_evpn_process_neigh_on_local_mac_change`). Measure with the stress
test: `ip_not_learned` should drop to about 0 without any delay.
**Status: open** (Phase 5 candidate).
- **Considered and not recommended:** advertising the remote-known MAC+IP
  immediately when the MAC becomes local, without confirming it with the
  host. It removes the race but can advertise a wrong binding if the
  host's IP or MAC changed.

16\. **Compare the controller on its own subnet against the robots' subnet** **[Done]**
(`CTRLER_SUBNET`).
**Status: done** (`stress_separate`, `stress_shared`). The IP is missed
either way, but only routed traffic loses packets.

15\. **Concurrent moves** **[Proposed, not built]** (`MOVE_BURST`: groups of robots moving at the same
instant), with a BGP capture to count routes per UPDATE, to see whether
batching changes UPDATEs per move. **Status: proposed, not built.**

14\. **Control-plane load metric:** **[Proposed, not built]** sample `bgpd`/`zebra` CPU and memory during
runs, for the proposal's "control-plane load" metric. **Status: proposed,
not built.**

13\. **Packet-capture option** **[Proposed, not built]** (`CAPTURE=1`: BGP on the spine links, VXLAN,
ARP on robot ports), needed for steps 12 and 15. **Status: proposed, not
built.**

12\. **Confirm the anycast explanation for lost traffic** with a packet capture
on a non-local leaf during a failed move, and **confirm the wasted
UPDATEs** **[Open]** (leaves echoing robot routes back to the other spine) with
`debug bgp updates` on leaf5. **Status: open.**

11\. **Investigate the IP-learning failure.** **[Done]**
**Status: done.** The stress test showed moves where the fabric learns the
robot's MAC but not its IP. Root cause confirmed in FRR and Linux source:
zebra's remote `NOARP` ARP entries, the kernel ignoring ARP for them, and
zebra's "TBD" for remote entries when a MAC becomes local. See
[ip_mobility_problem.md](ip_mobility_problem.md).

#### Original plan (oldest)

The first version of the plan, steps 1–10, kept in its original order.

1. **Write a measurable problem statement.** **[Open]** Turn the proposal into something
   testable, for example: "At the proposal's small-warehouse load (100 robots,
   several moves per robot per minute, on the lab's 4 access points), and when
   extrapolated to the large-warehouse scale, EVPN MAC mobility produces
   rising BGP update load, longer convergence and reachability gaps, and stale
   or inconsistent location state." Confirm this
   framing with whoever assigned the task, since it defines what done means.

   **Status: open.** Not yet confirmed with whoever assigned the task.
   The runs so far found a more specific problem than this framing expected:
   the new leaf failing to learn a moved robot's IP (see step 11).

2. **Make sure the current single-move test passes.** **[Open]** Get
   `tests/topotests/bgp_evpn_capstone/test_evpn_capstone_mobility.py` passing
   reliably first, and settle any uncommitted changes so the starting point is
   fixed.

   ```bash
   cd ~/frr/tests/topotests && sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_mobility.py
   ```

   **Status: open.** The test exists and runs, but
   `test_evpn_capstone_mobility.py` still has uncommitted changes in the
   working copy, so the starting point isn't fixed yet. The committed version
   moves make-before-break and waits up to 90 s for leaf5's `/32`
   (`count=90`), which would hide the IP-learning failure (see step 11).

3. **Build a stress test.** **[Done]** Write one parameterised test,
   `test_evpn_capstone_mobility_stress.py`, based on the existing one:
   - **Robots:** leaf1–4 (all on L2VNI 1000) are the four "access points",
     using the checked-in topology and configs. Each robot is cabled to all
     four, with one link live at a time.
   - **Parameters** (environment variables): `MV_INT`, `MV_COUNT`,
     `NUM_ROBOTS`, move order (round-robin or random), `DAD=on|off`. No
     topology parameters. With many robots, start their moves at staggered times so
     the fabric sees a steady rate (e.g. 100 robots every 20 s = 5 moves/s),
     not all at once.
   - **Metrics**, written to a CSV per move in `tgen.logdir`:
     - convergence time: robot disconnect until every leaf has the new
       location (MAC local on the new leaf, remote via the new VTEP on the
       others, `/32` via the new VTEP on leaf5);
     - EVPN UPDATE messages per move;
     - MAC mobility sequence number on each leaf;
     - DAD trips (for runs with DAD on).
   - **Reachability:** a background ping from `controller` to each robot every
     100–200 ms, to measure the reachability gap.
   - Use timestamped `bridge monitor fdb` and `ip monitor route vrf VRF500` on
     each leaf rather than polling vtysh, which adds hundreds of ms of error.
     All namespaces share the VM clock, so timestamps compare directly.

   **Status: done** (not committed).
   `test_evpn_capstone_mobility_stress.py` exists. Differences from the plan
   above:
   - **Parameters as built:** `NUM_ROBOTS`, `MV_COUNT`, `MV_INT`, `MV_ORDER`,
     `MV_MODE` (bbm/mbb), `LEARN`, `DAD`, `PING_INT`, `CONVERGE_TIMEOUT`,
     `SEED`, `VIS`, `ROUTE_SAMPLER`, `CTRLER_SUBNET`, `ATTACH_DELAY`.
   - **Metrics added:** ping loss and longest silence per move, `ip_learn_s`
     (when the new leaf learned the robot's IP), a raw `events.csv` timeline,
     `final_state.txt`, `route_samples.csv`.
   - **UPDATE counts** are per router over the whole run (FRR's counters), not
     per move. Measured: 52–60 UPDATEs per move fabric-wide, far more than the
     "about 8 per leaf" estimate in the target-scale section.
   - **Sequence numbers** are checked at the end of the run, not after every
     move (see step 7).

4. **Get a slow baseline.** **[Open]** One robot, one move every 60 s, about 20 moves.
   Everything should look healthy here, and every later run is compared
   against it.

   ```bash
   cd ~/frr/tests/topotests && sudo -E MV_COUNT=20 MV_INT=60 python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_mobility_stress.py
   ```

   **Status: open.** Earlier single-robot runs were disregarded
   (parameters not saved); no valid baseline run yet.

5. **Increase move rate.** **[Open]** Run one robot at 30, 20, 15, 10 and 5 s intervals.
   20 s is the assumed warehouse rate (3/min); 10 s (6/min) is the top of
   "several times per minute".
   - With DAD off: watch convergence time and reachability gap as the
     interval approaches the convergence time.
   - One run with DAD on (defaults): DAD should trip around move 5 once the
     interval drops below 36 s (5 moves in 180 s). This is the evidence for
     the DAD decision above.

   **Status: open.** An earlier move-rate sweep (8 robots, 20/10/5 s)
   was disregarded because its parameters weren't saved; it needs redoing.
   The DAD-on run hasn't been done. Note from the code: zebra only counts a
   move for DAD when the new leaf still has the MAC as remote
   (`zebra/zebra_evpn_mac.c:2284`), which break-before-make moves often avoid,
   so "DAD trips around move 5" may not hold in bbm.

6. **Scale up to the small warehouse.** **[Open]** Keep the 4 robot leaves and the 20 s
   interval, and grow the robot count in stages:

   | Stage | Robots | Robots per leaf | Fabric moves/s |
   |---|---|---|---|
   | A | 10 | 2.5 | 0.5 |
   | B | 40 | 10 (the proposal's ratio) | 2 |
   | C | 100 | 25 | 5 (small warehouse) |

   Then rerun stage C at 10 s (10 moves/s). Expected: updates per second grow
   with robots × move rate, and convergence time and `bgpd`/`zebra` CPU rise.
   Compare the measured per-leaf update rate and route count with the
   estimates in the target-scale table. Note where the VM itself becomes the
   limit; that's a lab limit, not necessarily an EVPN one.

   **Status: open.** All valid runs so far use 8 robots.

7. **Check sequence-number consistency.** **[Partly done]** After each move, snapshot
   `show evpn mac vni 1000 mac <mac> json` on every leaf. Flag moves where the
   sequence number didn't rise by exactly one, leaves still showing an old
   VTEP after convergence, and any time two leaves claim the robot at once.
   Rapid back-and-forth moves (faster than convergence) are the most likely
   trigger.

   **Status: partly done.** The stress test checks sequence numbers at
   the end of each run (`sequence.csv`): 0 mismatches in all valid runs.
   Per-move snapshots aren't implemented. Finding: in break-before-make the
   sequence number often doesn't rise on a move, because the old route is
   already withdrawn when the new leaf learns the MAC (valid under RFC 7432 §15, the base EVPN standard's MAC mobility
   rules).

8. **Repeat runs and plot.** **[Open]** Do 3–5 runs per setting and report median and
   spread. Plot each metric against move rate and robot count, and mark where
   things break: convergence longer than the gap between moves, update rate
   or CPU saturating, sequence inconsistencies appearing.

   **Status: open.** One run per setting so far.

9. **Extrapolate to the large warehouse.** **[Open]** Fit how each metric grew with
   robot count across stages A–C and project to 50,000 robots at
   3 moves/min. Estimate the effect of 5,000 leaves separately from the
   per-leaf numbers, since the leaf count is fixed in the lab. State the
   projected per-leaf update rate, route-table size and control-plane CPU,
   and whether they exceed what a leaf handled in the lab. Say clearly that
   this is a projection, not a measurement.

   **Status: open.** Needs step 6 first.

10. **Write it up** **[In progress]** under `doc/capstone/claude/`:
    - the problem statement and target scale;
    - the setup and how a move is modelled (make-before-break or
      break-before-make);
    - the DAD decision and its trade-off;
    - the metrics and how they're measured;
    - the results and breaking points, measured at small-warehouse load;
    - the large-warehouse projection and its assumptions;
    - the RFC sections each result traces back to (RFC 7432 §15 MAC Mobility
      and §15.1 MAC Duplication Issue, from the base EVPN standard; RFC 9721
      §6.8 Mobility Convergence and §8 Duplicate Host Detection, from Extended
      Mobility Procedures for EVPN-IRB);
    - the lab's limits: one VM shares CPU across all nodes, no real Wi-Fi
      roaming delay, scale far below 5,000 access points and 50,000 robots.

    **Status: in progress.** Current write-ups:
    [mobility_reproduction_findings.md](mobility_reproduction_findings.md),
    [ip_mobility_problem.md](ip_mobility_problem.md),
    [move_breakdown.md](move_breakdown.md).

Steps 3–5 are the minimum convincing reproduction. Steps 6–9 tie it to the
proposal's warehouse sizes.

### Break-before-make (newest first)

26\. **Delay control at 0.025 s** (`stress_delay_0.025s`) and **per-move race
margin.** **[Done]** **Status: done.** 1 of 48 moves missed the IP (id 40: the withdraw
took 136 ms while the robot arrived after 124 ms), with 3 moves showing a
negative margin (new leaf's entry removed after the robot attached). Together
with step 25 this puts the threshold between +25 and +50 ms of extra delay at
this load. The race margin (entry removed → robot attached, per move) and the
count of negative margins turned out to be the clearest view of the race;
proposed as a `moves.csv` column. Next: repeat 0.025 s and 0.05 s to get miss
rates rather than single counts.

25\. **Delay control at 0.05 s** **[Done]** (`stress_delay_0.05s`). **Status: done.** 0 of
48 moves missed the IP. The delay shifted the robot's arrival to 169 ms
(median, after the old leaf deleted its MAC) against withdraws of 114–144 ms,
so it always lost the race to the withdraw, but with as little as 18 ms to
spare. Outage per move ~0.28 s (median longest silence), no worse than a
converged move without a delay. Next: repeat at 0.05 s and try 0.02 s, and
under load, to see how often that margin runs out.

24\. **Withdraw latency vs robot arrival under load.** **[Partly done]** The withdraw latency
(old leaf deletes the robot's MAC → new leaf drops its remote IP entry) can be
measured from `events.csv`. In all four valid runs it was a steady ~120–130 ms
median (111–192 ms), the same as the time the robot normally takes to
reappear (~107–115 ms), which explains why the race is close. Next: add it as
a per-move column in `moves.csv` and measure it with more robots or a shorter
`MV_INT`, to see whether load stretches it. As an improvement target it's
limited: a faster withdraw narrows the race in break-before-make but can't
help make-before-break, where the withdraw only starts after the robot
arrives. **Status: partly done** (measured offline for the valid runs; no
column yet; all runs at the same load).

23\. **Delay control at 0.3 s** **[Done]** (`stress_delay_0.3s`). **Status: done.** 0 of
48 moves missed the IP; the remote entry was gone 251–421 ms before the robot
attached. Outage per move ~0.46 s (median longest silence), against ~0.71 s
at 0.5 s.

21\. **`ATTACH_DELAY` sweep** **[Partly done]** (0.1, 0.2, 0.3 s) to find the smallest gap that
avoids the race, i.e. how long the withdraw actually takes. **Status:
open.**
**Status: partly done.** 0.3 s also avoided the race (step 23). The
robot arrives ~0.12–0.16 s after the delay setting (measured from the old
leaf deleting its MAC), so 0.1 s would put arrival at ~0.22 s, just past the
slowest withdraw seen (192 ms, step 24): possibly enough, with little margin.
0.1 s and 0.2 s runs are still open.
**Status:** 0.05 s also avoided the race (step 25), so 0.1 s and 0.2 s
would add little; the open question is how small the margin can get (step
25).

19\. **`ATTACH_DELAY` control: keep the robot off the network long enough for
the withdraw to arrive.** **[Done]**
**Status: done** (`stress_delay`, 0.5 s). 0 of 48 moves missed the IP,
which confirms the race as the cause. Not a fix: it relies on the old port
going down, which a real warehouse wouldn't do.
**Status:** the run's folder is now `stress_delay_0.5s`.

## Target scale: the proposal's example warehouses

The proposal gives two example sizes and a move rate. These set the target
for the reproduction:

| | Small warehouse | Large warehouse |
|---|---|---|
| Access points (leaves) | 10 | 5,000 |
| Robots | 100 | 50,000 |
| Robots per access point | 10 | 10 |
| Moves per robot per minute | 3 (assumed) | 3 (assumed) |
| Moves per second, whole fabric | 5 | 2,500 |
| Route changes received per leaf per second (estimate) | ~40 | ~20,000 |
| Type-2 routes per leaf (estimate) | ~200 | ~100,000 |

How the numbers are derived:

- **Move rate:** the proposal says robots move "several times per minute".
  3/min (one move every 20 s) is the working assumption. Sweep 1–6/min
  (60 s to 10 s intervals) to cover "several".
- **Route changes per move:** the new leaf advertises 2 Type-2 routes (MAC
  and MAC+IP) and the old leaf withdraws 2. Each leaf receives each change
  from both spines, so it handles about 8 per move. Confirm this in step 3
  before relying on the per-leaf estimate.
- **Routes per leaf:** 2 Type-2 routes per robot, assuming every leaf imports
  the single robot VNI (1000), as in the current topology.

How the lab uses them:

- **The topology is fixed.** Every test uses the checked-in fabric: 2 spines,
  leaf1–4 as the robot access points, leaf5 for the controller. Only the
  number of robots varies.
- **Small warehouse: match it by robot count, not access-point count.** Run
  100 robots on the 4 robot leaves (25 per leaf). That matches the small
  warehouse's robot count, fabric-wide move rate (5/s at 3 moves/min) and
  Type-2 routes per leaf (every leaf imports VNI 1000). It doesn't match the
  access-point count: 4 instead of 10, so each update reaches fewer leaves.
- **Large warehouse: extrapolate.** It can't run on one VM. Measure how each
  metric scales from 1 to 100 robots, then project to 50,000 robots at
  3 moves/min. The effect of more leaves can't be measured in this topology,
  so estimate it from the measured per-leaf numbers (e.g. updates per move
  scale with the number of leaves that import the VNI) and say it's an
  estimate.
- **Cost of 100 robots:** every robot is cabled to all 4 robot leaves, so
  100 robots is 400 access links. Setup will be slow but should work; measure
  it in the first large run.

## Decision: duplicate address detection (DAD) is off

Per customer direction, DAD is disabled for the reproduction runs.

- **What it protects against:** duplicate MACs or IPs (cloned images, mistyped
  static IPs, swapped NICs), layer-2 loops that make a MAC flap and flood the
  fabric with BGP updates, and spoofed addresses (RFC 9721 §9, Security Considerations of
  Extended Mobility Procedures for EVPN-IRB, which covers mobility spoofing).
- **What we give up:** all of the above go undetected. This is acceptable if
  robot addresses are centrally assigned. A loop could still cause an update
  storm with nothing to stop it.
- **Scope:** FRR configures DAD once per EVPN instance
  (`bgpd/bgp_evpn_vty.c:4119`). Disabling it covers every host, including the
  controller. It can't be disabled for robots only without a code change.
- **Config:** on each leaf, under the default BGP instance (the one with
  `advertise-all-vni`):

  ```
  router bgp 6501N
   address-family l2vpn evpn
    no dup-addr-detection
  ```

  Verify with `show evpn` on each leaf. `show running-config` should list
  `no dup-addr-detection`.
- **Handling:** make DAD a test setting (`DAD=on|off`, default `off`) rather
  than a hard-coded change, and keep one run with defaults as evidence of why
  disabling was needed (see step 5).
  **Status:** `DAD=on|off` is implemented (default off); the run with
  defaults hasn't been done yet.
- **Follow-up:** without DAD nothing watches for unusual move rates. A per-MAC
  move-rate alarm (monitoring path) or sticky MACs for fixed devices like the
  controller would cover the loop and duplicate cases.
