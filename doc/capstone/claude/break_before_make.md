# Break-Before-Make MAC Mobility: Predictions and Measurements

This document looks at changing how the fabric handles a robot move, from
the current make-before-break behaviour to break-before-make (BBM). It has
four parts:

1. What break-before-make means, and the problems we expected from it.
2. How the BBM simulation in
   [test_evpn_capstone_warehouse.py](../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone_warehouse.py)
   works and how to run it.
3. What the runs showed, prediction by prediction. Some predictions held up
   and some did not.
4. Background on silent mode, radios and gaps, then each prediction again,
   step by step, with an analogy.

It follows on from
[ip_relearning_root_cause.md](ip_relearning_root_cause.md) (why the robot's
IP isn't re-learned, which is requirement 3) and
[warehouse_requirements_results.md](warehouse_requirements_results.md) (what
each requirement means). The runs were done on 2026-11-18.

## Part 1: What break-before-make means

**Make-before-break (today).** The robot shows up at the new leaf while the
old leaf still has it. The new leaf advertises the robot with a higher
sequence number, every leaf picks the newer route, and the old leaf then
withdraws its own. This is the RFC 7432 §15 MAC mobility procedure.

**Break-before-make.** The old leaf removes the robot and withdraws its
routes first. When the robot shows up at the new leaf, the fabric treats it
as a brand-new host, not a move.

The warehouse test's default mode does neither cleanly. The radio goes off,
but the old leaf's port stays up, so the old leaf never learns the robot
left. That is a **silent break**. True BBM needs the old leaf to find out,
for example from the access point (AP) or wireless controller.

### What we expected BBM to fix

1. **Requirement 3, if the withdrawal arrives first.** The old leaf's MAC+IP
   withdrawal makes the new leaf delete its `NOARP` copy of the robot's IP.
   The robot's ARP then creates a normal local entry, and zebra advertises
   MAC+IP. In the reception-desk analogy, the stamped note is gone before
   the robot arrives, so the clerk writes a pencil note.
2. **No sequence-number contest.** The old route is gone, so the new leaf
   advertises a fresh route and has nothing to outbid.
3. **Duplicate-address detection (DAD) probably stops firing.** FRR counts a
   move when a MAC is learned locally while a remote entry exists. If the
   remote entry is already deleted, there is no move to count.

### New problems we expected

1. **A longer gap.** The withdrawal and the new advertisement would happen
   one after the other, so the outage would stack two propagation times
   (roughly 300–500 ms or more).
2. **Detecting the break.** An AP's uplink port stays up when one client
   leaves, so the leaf needs a disassociation event that EVPN has no
   standard way to receive. The fallbacks are slow: MAC ageing takes
   minutes, ARP probing takes seconds.
3. **A race with the robot.** Wi-Fi roaming takes tens of milliseconds, and
   a BGP withdrawal takes hundreds. If the robot arrives first, the new leaf
   still has the stamped note, and requirement 3 comes back.
4. **Flooding during the gap.** Remote leaves no longer know where the robot
   is, so they flood bridged traffic to every leaf as unknown unicast.
   Routed traffic falls back to the /24 route and is spread across leaf1–4.
5. **Stale routes can win.** A fresh route starts at sequence 0. Any leaf
   that still holds the old route with a higher number, because its
   withdrawal was late or lost, keeps preferring the old location.
6. **False breaks.** A short signal dropout looks the same as leaving, so
   the robot is withdrawn and re-advertised even though it never moved.
7. **Lost mobility history.** Sequence numbers and DAD no longer see moves,
   which also weakens loop detection and makes MAC takeover easier.
8. **Twice the BGP churn.** Each move would be a withdrawal plus a new
   advertisement instead of one update.
9. **In-flight packets are dropped.** Nothing forwards traffic from the old
   leaf to the new one during the gap.

### Problems we expected to stay

| Problem | Expected with BBM |
|---|---|
| Loss per move | Worse (two propagations in a row) |
| Hosts must announce themselves | Unchanged: the new leaf still needs a frame from the robot |
| Timer mismatches | Unchanged, plus a detection timer on the old leaf |
| Scale and churn | Worse (about twice the updates) |
| Security | Somewhat worse (easier MAC takeover) |
| Interop | Allowed by the standard, but depends on non-standard detection |

## Part 2: The simulation

### What BBM mode does

`WAREHOUSE_ROAM_MODE=bbm` changes each move into three steps:

1. **Break.** The radio goes off at the old AP. The old leaf then drops the
   robot at once, as if the AP had reported the disassociation:
   `ip neigh del 45.0.0.11 dev br1000` and
   `bridge fdb del 00:00:00:00:00:11 dev eth-host11 master`. zebra sees both
   go and withdraws the robot's routes.
2. **Gap.** The test waits for the configured gap.
3. **Make.** The radio comes on at the new AP and the robot pings its
   gateway once, the same as in silent mode.

The old leaf finds out instantly, which is the best case for BBM. A real
fabric would also have to wait for detection.

The gap comes from `WAREHOUSE_ROAM_GAPS`, one value per pair of moves, so
each gap is tried once in each direction. A configured gap of 0 still takes
about 50–60 ms, because of the commands that run between break and make. That
is close to a real Wi-Fi roam.

The same three steps run in the default silent mode, minus the old leaf
dropping the robot. A silent-mode run with the same gaps is the comparison.

### What it measures

Each item below targets one of the predictions in Part 1.

| Measurement | Prediction it tests | Column / field |
|---|---|---|
| Outage, and outage beyond the gap | 1 (longer gap), 9 (in-flight loss) | `outage_ms`, `beyond_gap` |
| Time until no leaf points at the old leaf | 3 (race) | `old_gone_ms` |
| Whether the new leaf still had the `NOARP` copy when the robot arrived | 3 (race), fix 1 | `stale_copy` |
| Whether the routed controller lost the robot until the next move | fix 1 (requirement 3) | `routed ref` / `routed_ref_unrecovered` |
| Probes arriving at leaf3/leaf4, counted by `tc` on their VXLAN devices | 4 (flooding) | `bystander` (bridged/routed) |
| BGP UPDATE messages received by leaf3 per move | 8 (churn) | `bgp_rx` |
| New leaf's sequence number, DAD flag | fixes 2 and 3, problem 7 | `seq`, `duplicate flagged on` |
| Signal dropouts without a move | 6 (false breaks) | `test_signal_dropouts` table |

For the bystander counts, VNI 1000 carries bridged probes from `ctrl45` and
the L3VNI carries routed probes from `controller`. leaf3 and leaf4 are never
the robot's AP, so any probe that reaches them was flooded or sprayed. With
the robot standing still, they receive zero.

### How to run

```bash
cd /home/frr/frr/tests/topotests

# BBM with a spread of gaps (default gaps 0, 0.25, 0.5, 1, 2 s)
WAREHOUSE_ROAM_MODE=bbm WAREHOUSE_CONTROLLER=same-subnet \
  sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_warehouse.py

# Silent mode with the same gaps, for comparison
WAREHOUSE_ROAM_MODE=silent WAREHOUSE_ROAM_GAPS=0,0.25,0.5,1,2 WAREHOUSE_CONTROLLER=same-subnet \
  sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_warehouse.py

# BBM with small gaps, to find where the race is lost
WAREHOUSE_ROAM_MODE=bbm WAREHOUSE_ROAM_GAPS=0,0.1,0.2 WAREHOUSE_MOVES=12 WAREHOUSE_DROPOUTS=0 \
  WAREHOUSE_CONTROLLER=same-subnet \
  sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_warehouse.py
```

`WAREHOUSE_CONTROLLER=same-subnet` runs both controllers at once: `ctrl45`
(bridged, measured for the requirements table) and `controller` (routed,
reported as `routed ref`). Each run takes about 4 minutes. The tables print
to the console, and everything is saved to `warehouse_results.json` in the
log directory. The new fields are under `moves[]`, `dropouts[]` and
`roam_evidence`.

## Part 3: What the runs showed

Four runs, all with the same-subnet controller and 5 moves per minute:

| Run | Mode | Gaps (s) | Moves |
|---|---|---|---|
| A | BBM | 0, 0.25, 0.5, 1, 2 | 10 |
| B | silent | 0, 0.25, 0.5, 1, 2 | 10 |
| C | BBM | 0, 0.1, 0.2 | 12 |
| D | silent | dropouts only | 0 |

All tests passed in every run; the warehouse test reports results rather
than failing on unmet requirements.

### Summary

| # | Prediction | Result |
|---|---|---|
| Fix 1 | BBM fixes requirement 3 if the withdrawal wins the race | **Confirmed**, but only when the robot takes ≥ ~180 ms to reappear |
| Fix 2 | No sequence-number contest | **Confirmed** for gaps ≥ ~180 ms |
| Fix 3 | DAD stops firing | **Confirmed** |
| 1 | Longer outage (two propagations stacked) | **Not confirmed.** The outage is about the gap itself |
| 2 | Detecting the break is hard | Not testable here; the simulation detects instantly |
| 3 | Race with the robot | **Confirmed.** A realistic roam lost the race 3 of 3 times |
| 4 | Flooding during the gap | **Confirmed** |
| 5 | Stale routes can win | Not tested |
| 6 | False breaks cause churn and outage | **Confirmed** for BGP churn and routed traffic |
| 7 | Lost mobility history | **Confirmed** |
| 8 | Twice the BGP churn | **Not confirmed.** About the same as silent mode |
| 9 | In-flight packets dropped | **Confirmed** (and also true in silent mode) |

### Requirement 3 and the race (fix 1, problem 3)

The routed controller loses the robot until its next move when requirement 3
fails. Counting leaf1 → leaf2 moves, the only direction where it fails:

| Mode | Actual gap | Moves where the routed controller lost the robot |
|---|---|---|
| Silent (B) | 49 ms – 2.05 s | **5 of 5**, at every gap |
| BBM (A, C) | 52–62 ms ("gap 0") | **3 of 3** |
| BBM (A, C) | 180 ms – 2.07 s | **0 of 8** |

- **Silent mode** loses at every gap. Waiting longer doesn't help, because
  the old leaf never withdraws on its own.
- **BBM** fixes requirement 3 when the robot takes about 180 ms or more to
  reappear. That gave the withdrawal time to delete the new leaf's copy.
- **At a realistic roam time** (about 50–60 ms), BBM lost the race every
  time. The new leaf still had the `NOARP` copy when the robot arrived
  (`stale_copy` = yes), and the routed controller was still cut off at the
  end of the 12 s window (35–118 missed instructions).
- **The withdrawal took a median of 288 ms (A) and 356 ms (C)** to reach
  every leaf (`old_gone_ms`), with a worst case of 1,074 ms. The robot has
  to stay off the network at least that long for BBM to fix requirement 3
  reliably.

The threshold lies between about 60 ms and 180 ms in this lab. These are
small samples, so treat the boundary as rough.

One result isn't explained yet. In run C, moves 3 and 9 had
`stale_copy` = yes but still recovered. The copy was probably deleted by the
withdrawal shortly after the robot arrived, and a later ARP exchange
re-learned the IP. That wasn't traced.

### Outage per move (problems 1 and 9)

The prediction was that BBM would add a second propagation time to every
move. It didn't.

| Mode | Outage beyond the gap, median | Worst |
|---|---|---|
| Silent (B) | 167 ms | 548 ms |
| BBM (A) | 16 ms | 123 ms |
| BBM (C) | 39 ms | 113 ms |

With a gap of 0, the total outage was similar in both modes: 150–188 ms for
BBM and 200 ms for silent.

**Why the prediction was wrong.** After the withdrawal, remote leaves don't
know where the robot is, so they flood its traffic to every leaf, including
the new one. As soon as the radio comes back, the flooded traffic reaches
the robot, before any new route has spread. In silent mode, traffic keeps
going to the old leaf until the new route arrives, so the second
propagation shows up there instead. Flooding is what hides the cost
(problem 4).

**What is confirmed: the whole gap is lost** (problem 9). In both modes, the
outage is at least the gap, and missed instructions grow with it: at a 2 s
gap, BBM missed 20–22 instructions and silent mode 22–27. Under BBM, the gap
is set by detection time plus roam time, so a slow detector translates
directly into outage.

**The real trade-off:** BBM needs a gap of about 180 ms or more to win the
race. At gaps of about 180–300 ms, the outage was 160–410 ms per move.
That is in the same range as silent mode at gap 0 (about 200 ms) or
somewhat worse, but with requirement 3 fixed. That only
holds if detection is instant, as it is in this simulation.

### Flooding (problem 4)

Probes that reached leaf3 and leaf4, which are never the robot's AP:

| Run | Bridged (`ctrl45`) | Routed (`controller`) |
|---|---|---|
| Baseline, no moves | 0 | 0 |
| Silent (B), 10 moves | 24 | 6,050 |
| BBM (A), 10 moves | **2,696** | 2,009 |
| BBM (C), 12 moves | **1,233** | 2,655 |

- **Bridged flooding is a BBM effect.** Silent mode sent 24 bridged probes
  to bystanders across 10 moves; BBM sent over 2,600. It grows with the gap:
  in run A, the 2 s moves alone sent 636–685 each.
- **Routed spray comes from requirement 3** in both modes. With no /32
  route, leaf5 spreads routed traffic over leaf1–4 using the /24 route. In
  silent mode that happens on every leaf1 → leaf2 move, which is why silent
  mode has the larger routed number. In BBM, most of it (about 1,170 per
  move) came from the gap-0 moves that lost the race.

At one robot this is harmless. At warehouse scale, every roaming robot would
flood its traffic to every leaf in its VNI for the length of its gap.

### Sequence numbers and DAD (fixes 2 and 3, problem 7)

| Mode | New leaf's sequence number after each move | DAD |
|---|---|---|
| Silent (B) | 1, 2, 3, … 10 | Flagged after move 5 |
| BBM (A) | 1, 2, then 0 for every later move | Never flagged |
| BBM (C) | 1, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0 | Never flagged |

- **With a large enough gap, every move looks like a brand-new host** at
  sequence 0, so DAD never sees a move. That fixes the requirement 5 failure
  (DAD flagging the robot).
- **The same mechanism blinds the fabric.** Sequence numbers no longer
  count moves, and DAD (which also catches loops and duplicate MACs) never
  triggers. The sequence numbers only increased on the gap-0 moves, where
  the robot arrived before the withdrawal, so the fabric still saw a move.

### False breaks (problem 6)

`test_signal_dropouts`: three 0.5 s signal dropouts at leaf1, with the robot
not moving and not sending anything when its signal returns.

| Mode | BGP updates at leaf3 per dropout | Bridged missed | Routed missed |
|---|---|---|---|
| Silent (D) | 0 | 5 | 5 |
| BBM (A) | 6 | 4–5 | **11** |

- **In silent mode, a dropout is invisible to the fabric.** No routes change,
  and traffic resumes as soon as the signal does.
- **In BBM, each dropout withdraws and re-advertises the robot:** 6 BGP
  updates for a robot that never moved. Bridged traffic still recovered
  right away because of flooding. Routed traffic missed about 6 more
  instructions (about 600 ms) beyond the dropout, while the IP was
  re-learned.

### BGP churn (problem 8)

| Mode | BGP updates at leaf3 per move | Total, 10 moves |
|---|---|---|
| Silent (B) | 4 (leaf1 → leaf2), 8 (leaf2 → leaf1) | 60 |
| BBM (A) | 4 on the first move, then 6 | 58 |

The prediction was wrong. In make-before-break, the old leaf also withdraws
once it sees the higher sequence number, so both modes send an advertisement
and a withdrawal per move. The message count is about the same. Churn
scaling with the number of robots is still a concern, but BBM doesn't make
it worse. False breaks do add churn that silent mode doesn't have.

### Detection (problem 2)

The simulation can't test detection, because it tells the old leaf
instantly. Two pieces of data show what a real fabric would have to deal
with:

- **In silent mode, the old leaf never finds out** that the robot left. Its
  port stays up, and it keeps the robot as local until another leaf
  advertises a higher sequence number.
- **The old leaf's bridge MAC ageing time is 300 s** (from the run's
  config). Without an event from the AP, that is how long the old leaf
  could keep the robot.

So every BBM benefit above depends on a fast, reliable signal from the AP or
wireless controller to the leaf. EVPN doesn't define one.

### Not tested

- **Stale routes winning (problem 5).** It would need a withdrawal to be
  lost or delayed on purpose.
- **Scale.** One robot only. The flooding and churn numbers are per robot
  and would multiply.
- **Real detection delay.** The simulation is the best case.

## Part 4: Step by step, with an analogy

### Background: silent mode, radios and gaps

**Silent mode** is the test's default way of moving the robot. The radio
turns off at the leaf1 access point and on at the leaf2 access point, and
the robot pings its gateway once to announce itself. Nobody tells leaf1 the
robot left: its port stays up and it still lists the robot as connected. It
only finds out when leaf2 advertises the robot with a higher sequence
number, which is the normal make-before-break process. It's called silent
because the robot leaves without leaf1 hearing about it.

Silent mode has two purposes:

- **It matches real Wi-Fi.** An AP's cable to the leaf stays up when one
  client drives away, because the AP still serves other clients. A real
  leaf usually gets no signal that a robot left.
- **It's the baseline.** Every earlier result, including the requirement 3
  failure, came from silent mode. BBM only means something when compared
  against silent mode under the same conditions.

**Radios.** In a real warehouse, a robot talks to an AP over Wi-Fi, and the
AP is wired to a leaf. When the robot drives out of range of one AP and into
range of another, it roams. The lab has no Wi-Fi, so it fakes this:

- **The two APs:** the robot (host11) has two cables, one to leaf1 and one
  to leaf2. Both stay up the whole run, like two APs that are always on.
- **The radio:** a `tc` filter on the robot's side of each cable, not real
  hardware. With the radio on, packets pass. With it off, the filter drops
  everything in both directions, so the robot is out of range of that AP.

Unplugging the cable instead would put the leaf's port down, and the leaf
would learn instantly that the robot left. That's unrealistic for Wi-Fi, and
it's a form of break-before-make, which is the thing being tested
separately. The radio filter keeps the leaf unaware.

**Gaps.** The gap is how long the robot is off the air between leaving the
old AP and joining the new one. BBM was run with several gaps for three
reasons:

1. **BBM's result depends on a race, and the gap decides who wins.**
   Leaf1's withdrawal has to reach leaf2 before the robot does. A single gap
   would mislead either way:
   - Testing only gap 0 would suggest BBM never fixes requirement 3, because
     the robot wins the race every time.
   - Testing only 2 s would suggest it always does, because the withdrawal
     wins every time.

   The sweep showed where the outcome flips, between about 60 ms and 180 ms.
2. **Real gaps vary.** A fast Wi-Fi roam takes tens of milliseconds; a slow
   one, where the robot scans for a new AP, can take seconds. In real BBM,
   the gap also includes however long the old leaf takes to notice the robot
   left.
3. **It separates two kinds of outage.** While the radio is off, every
   instruction is lost whatever the fabric does. With several gaps, the
   outage splits into "the gap itself" and "time beyond the gap". Only the
   second part is the fabric's fault.

Silent mode was run with the same gaps, so each BBM result has a direct
comparison.

### The analogy

This extends the reception-desk analogy from
[ip_relearning_root_cause.md](ip_relearning_root_cause.md).

| Network | Analogy |
|---|---|
| Leaves 1–5 | Reception desks in an office building |
| Robot | An employee, Robot-11, who keeps changing desks |
| MAC table | The **face book**: "this face is in my hallway" or "…in desk 1's hallway" |
| Neighbor table | The **name book**: "the name Robot-11 belongs to this face" |
| Kernel-learned entry | A **pencil note** the clerk wrote after meeting the person |
| zebra's `NOARP` copy | A **stamped note** from head office. The clerk trusts it over anything the person says |
| BGP and the spines | **Head-office memos** passed between desks |
| leaf5's /32 route | The **mailroom directory**: "Robot-11 → desk 2". Without it, the mailroom sends letters to a random desk 1–4 |
| Instructions | Letters from the manager (controller), one every 100 ms |
| Flooding | Photocopying a letter to every desk, and each desk reading it out in its hallway |
| Radio off | The employee is in the elevator, where nobody can reach them |
| Silent mode | Robot-11 walks away without telling desk 1 |
| BBM | Desk 1 notices at once and sends a memo: "Robot-11 is gone, erase them" |

### A. BBM fixes requirement 3, but only if it wins a race

Fix 1 and problem 3.

**Step by step:**

1. The radio turns off at leaf1. Leaf1 deletes the robot's entries, and
   zebra tells BGP to withdraw both routes: MAC+IP and MAC-only.
2. The withdrawal travels through the spines to every leaf. Leaf2 deletes
   its `NOARP` copy of the robot's IP.
3. The robot's radio comes on at leaf2, and it sends an ARP for its gateway.
4. **If step 2 finished first:** leaf2 has no copy, so its kernel creates a
   normal entry from the ARP. zebra advertises MAC+IP, leaf5 gets a /32
   route, and routed traffic works.
5. **If the robot arrived first:** leaf2 still has the copy, so it ignores
   the ARP and advertises MAC-only. Then the withdrawal arrives and deletes
   the copy. Leaf2 has nothing, and the robot won't ARP again for a while.
   That's requirement 3 again.

**Measured:** `stale_copy` (whether the copy was still there when the robot
arrived), `old_gone_ms` (how long the withdrawal took to reach every leaf),
and whether the routed controller lost the robot.

**Result:**

| Mode | Robot back after | Routed controller lost the robot |
|---|---|---|
| Silent | any gap | 5 of 5 |
| BBM | 52–62 ms (realistic roam) | 3 of 3 |
| BBM | 180 ms or more | 0 of 8 |

The withdrawal took a median of 288–356 ms to reach every leaf.

**Analogy:** it's a race between head office's "erase Robot-11" memo and
Robot-11 walking to desk 2.

- **The memo wins:** desk 2's name book is blank. Robot-11 introduces
  themselves, and the clerk writes a pencil note.
- **Robot-11 wins:** the clerk sees the stamped note and ignores the
  introduction. Then the memo erases the stamped note, leaving a blank page,
  and Robot-11 has already finished introducing themselves.

A real roam is a sprint of about 50 ms, and the memo takes about 300 ms, so
in practice Robot-11 usually wins and the fix fails.

### B. No sequence-number contest, no duplicate detection, no move history

Fixes 2 and 3 and problem 7. They are one mechanism.

**Step by step:**

1. **Silent mode:** leaf2 learns the robot while leaf1's route still
   exists. That counts as a move, so the sequence number goes up by 1.
2. **Duplicate detection** counts those moves. Five in 180 s, and it flags
   the robot.
3. **BBM with a long enough gap:** when the robot arrives, no route exists
   anywhere. Leaf2 treats it as a brand-new host at sequence 0, and no move
   is counted.

**Result:** silent mode counted sequence numbers 1, 2, 3 … 10 and flagged
the robot at move 5. BBM stayed at sequence 0 on almost every move and never
flagged it. The exception was the gap-0 moves, which still went up by 1: the
robot beat the withdrawal, so the fabric still saw a move.

**Analogy:** think of a visitor log.

- **Silent mode:** Robot-11 checks in at desk 2 while still checked in at
  desk 1, so security logs a desk change. After five changes, security flags
  them as suspicious.
- **BBM:** desk 1 checks Robot-11 out first, so every check-in looks like a
  first visit. That's good for Robot-11, but security can no longer spot two
  people sharing one badge (a duplicate MAC) or someone walking in circles
  (a loop). Those are the problems duplicate detection exists to catch.

### C. The outage would be longer (prediction was wrong)

Problem 1.

**The prediction:** first the withdrawal spreads (about 300 ms), then the
new advertisement spreads (about 300 ms). One after the other, that roughly
doubles the outage.

**What actually happens:**

1. After the withdrawal, no leaf knows where the robot is, so leaves flood
   its traffic to every leaf, including leaf2.
2. When the radio comes on at leaf2, the robot immediately receives the
   flooded copies and replies, without waiting for any new route.
3. In silent mode, leaf5 keeps sending to leaf1, the stale location, until
   leaf2's advertisement arrives. That's where silent mode's extra time
   comes from.

**Result:** outage beyond the gap had a median of 16–39 ms in BBM, against
167 ms in silent mode (worst 548 ms).

**Analogy:**

- **BBM:** when no one knows where Robot-11 is, the mailroom photocopies
  each letter to every desk, and each desk reads it out in its hallway.
  Robot-11 hears it the moment they step into any hallway.
- **Silent mode:** the mailroom keeps sending letters to desk 1, whose
  hallway is empty, until the memo arrives saying "desk 2 now".

### D. Everything sent during the gap is lost

Problem 9.

**Step by step:**

1. While the radio is off, the robot can't receive anything, in either mode.
2. Nothing forwards packets from the old leaf to the new one, so they're
   dropped.

**Result:** with a 2 s gap, 20–27 instructions were missed, which is about
2 s ÷ 100 ms. This held in both modes. What BBM changes is how long the gap
is: it becomes the time to notice the robot left plus the roam time.

**Analogy:** Robot-11 is in the elevator. No desk can reach them, and
there's no service that holds their letters and hands them over at the next
floor.

### E. Flooding during the gap

Problem 4.

**Step by step:**

1. The withdrawal reaches leaf5, the controller's leaf. Leaf5 no longer
   knows where the robot's MAC is.
2. For bridged traffic, leaf5 floods every probe to every leaf as unknown
   unicast.
3. The test counted probes arriving at leaf3 and leaf4. They're never the
   robot's AP, so anything they receive was flooded.

**Result:**

| Situation | Bridged probes at leaf3/leaf4 |
|---|---|
| Robot standing still | 0 |
| Silent, 10 moves | 24 |
| BBM, 10 moves | 2,696 |

In BBM the count grows with the gap, to about 650 per move at a 2 s gap.
Routed probes were also sprayed to random leaves whenever the /32 route was
missing. That happens in both modes and comes from requirement 3, not BBM.

**Analogy:** photocopying one employee's letters to every desk is a minor
nuisance. Now do it for every employee who's between desks at any moment.

**At warehouse scale** (a back-of-envelope estimate, not measured): 50,000
robots × 5 moves/min is about 4,200 moves per second. With 200 ms gaps,
about 830 robots are in a gap at any moment, all being flooded to every
leaf.

### F. The leaf has to detect the break (not testable here)

Problem 2.

**Step by step:**

1. In the simulation, the test tells leaf1 instantly. That's the best case.
2. On real Wi-Fi, leaf1's port stays up, so leaf1 needs a message from the
   AP or wireless controller. EVPN has no standard way to receive one.
3. The fallback is MAC ageing, which the run recorded at 300 s on leaf1.

**Evidence:** in silent mode, leaf1 never found out on its own; it kept the
robot until leaf2 outbid it. Every BBM benefit above assumes instant
detection.

**Analogy:** desk 1's hallway door is always open. The clerk only knows
someone left if a hallway monitor (the AP) phones them. Otherwise the face
book entry fades after 5 minutes.

### G. Stale routes can win (not tested)

Problem 5.

**Step by step:**

1. Leaf4 misses or delays the withdrawal. It still holds "robot at leaf1,
   sequence 7".
2. Leaf2 advertises "robot here, sequence 0", because BBM made it a fresh
   host.
3. Leaf4 compares the two. 7 beats 0, so it keeps sending to leaf1 until the
   withdrawal finally arrives.

The test never loses a withdrawal on purpose, so this wasn't measured.

**Analogy:** desk 4 missed the "erase" memo. Its note saying "visit #7 at
desk 1" outranks a new one saying "visit #0 at desk 2".

### H. False breaks

Problem 6.

**Step by step:**

1. The robot's signal drops for 0.5 s, say behind a shelf, but it doesn't
   move.
2. **BBM:** leaf1 treats this as leaving. It withdraws the robot and
   re-learns it when the signal returns. Leaf5 loses its /32 route, and
   routed traffic waits for the IP to be re-learned.
3. **Silent:** nothing in the fabric changes. Traffic resumes when the
   signal does.

**Result (per dropout):**

| | Silent | BBM |
|---|---|---|
| BGP updates | 0 | 6 |
| Routed instructions missed | 5 | 11 (about 600 ms extra) |
| Bridged instructions missed | 4–5 | 4–5 (flooding covered it) |

**Analogy:** Robot-11 ducks behind a filing cabinet for half a second.

- **Silent mode:** nobody notices.
- **BBM:** desk 1 sends "Robot-11 is gone", then "Robot-11 is back". The
  mailroom erases its directory entry, and mail sorted by name is delayed
  until it's rewritten.

### I. Twice the BGP churn (prediction was wrong)

Problem 8.

**The prediction:** BBM sends a withdrawal plus an advertisement, while
silent mode sends one update.

**What actually happens:** silent mode also sends both. Leaf2 advertises,
then leaf1 withdraws once it sees the higher sequence number. Only the order
differs.

**Result:** over 10 moves, leaf3 received 58 updates in BBM and 60 in silent
mode.

**Analogy:** both ways take two memos, "I have Robot-11" and "I don't have
Robot-11". BBM just sends them in the opposite order.

### In short

BBM trades one problem for others.

- **What it fixes:** requirement 3 and duplicate detection, but only if the
  leaf detects the break instantly and the robot is slower than the
  withdrawal.
- **What it costs:** flooding, a fabric that can't see moves or loops, and
  extra work on every signal dropout.
- **What it doesn't fix:** the 100 ms service-gap requirement, in either
  mode.

## What this means

- **BBM can fix requirement 3,** but only if the robot stays off the network
  longer than the withdrawal takes to spread (about 180 ms or more here). A
  real Wi-Fi roam is faster than that, so it would lose the race unless the
  new leaf or the robot waits.
- **BBM fixes the DAD problem** by hiding moves from the fabric. That also
  hides loops and duplicate MACs.
- **BBM's outage is not worse than silent mode for the same gap,** because
  flooding delivers traffic during the transition. The price is flooding
  every roaming robot's traffic to every leaf.
- **Everything depends on detecting the break,** which EVPN doesn't
  standardize and which the simulation assumes is instant.
- **No mode meets the 100 ms service-gap requirement.** The best BBM moves
  with a gap ≥ 180 ms still lost about 160–410 ms.

## Caveats

- **One run per configuration** and small samples per gap (1–2 moves per
  direction). Treat thresholds as rough.
- **Namespace lab, not hardware.** Real switches add hardware programming
  time and have different BGP timing.
- **Control-plane times are good to about ±100 ms,** since each vtysh call
  takes about that long. Data-plane times are good to 10 ms.
- **Dropout windows were fixed after runs A and B.** A dropout's window
  could overlap the start of the next dropout, which put false "+"
  (unrecovered) flags in run B's dropout table. Run D was done after the
  fix and is the silent-mode comparison used above. Run A's dropout table
  had no flags, so its numbers are unaffected.
