# Break-before-make robot move: test data

Data from [test_evpn_capstone_break_before_make.py](../../../../tests/topotests/bgp_evpn_capstone/test_evpn_capstone_break_before_make.py),
collected on 2026-11-11 in the `frr-dev` container.

In every trial the robot moves from leaf1 to leaf2, **break before make**:

1. **Break:** the robot's link to leaf1 goes down. leaf1 sees the cable unplug,
   flushes the robot's MAC, and EVPN withdraws it.
2. **Gap:** the robot is connected to neither leaf.
3. **Make:** the robot's link to leaf2 comes up and it sends one ARP for its
   gateway.

The controller sends the robot an instruction every 100 ms (a UDP probe every
10 ms, every 10th one counts). An instruction is **missed** if it is lost or
takes over 100 ms. Every run starts with a 10 s baseline with the robot
standing still, and every baseline missed **0** instructions.

## Claims and where the data proves them

| Claim | Evidence |
|---|---|
| With break-before-make, the robot misses instructions for as long as it is offline: about 1 per 100 ms. | Run 1: the outage tracks the gap (0.5 s, 1 s, 3 s gaps → about 0.5, 1, 3 s outages and 5, 8–9, 29–30 missed). |
| Whether EVPN counts the move as a **move** (sequence number goes up) or a **new device** (sequence number 0) is a race between the robot reaching leaf2 and leaf1's withdraw reaching leaf2. | Run 2: 4 of 10 trials were moves, and those are the ones where leaf2 saw the robot quickest (112–178 ms); 6 of 10 were new devices (leaf2 saw it at 228–368 ms). |
| If the robot switches instantly, every move is counted as a move and almost no instructions are missed. | Run 3: 10 of 10 sequence numbers went up; median outage 34 ms; 8 of 10 trials missed 0 instructions, 2 missed 1. |
| Counting every move as a move trips duplicate-address detection when the robot moves often. | Run 3: leaf2 flagged the robot from trial 3 (its 5th move within 180 s); both leaves from trial 4. |

## The runs

### Run 1: gaps of 0, 0.5, 1 and 3 s — [run1_gaps_0_to_3s/](run1_gaps_0_to_3s/)

Two runs, one trial per gap. The robot switched links with a series of shell
commands, so the "0 s" gap really lasted 117–426 ms.

| Gap | Actual gap | Outage | Missed | leaf2 sequence |
|---|---|---|---|---|
| 0 s | 426 ms / 117 ms | 728 ms / 127 ms | 6 / 2 | 0 / 1 |
| 0.5 s | 527 ms / 516 ms | 451 ms / 502 ms | 5 / 5 | 0 / 0 |
| 1 s | 1027 ms / 1054 ms | 965 ms / 1051 ms | 9 / 8 | 0 / 0 |
| 3 s | 3018 ms / 3022 ms | 2999 ms / 3128 ms | 29 / 30 | 0 / 0 |

(first value: 19:03 run; second value: 19:51 run)

- `console_19-03.log`, `console_19-51.log`: the console output of each run,
  with its table.
- `19-51_trial2_gap0.5s.txt`, `19-51_trial3_gap1.0s.txt`,
  `19-51_trial4_gap3.0s.txt`: timelines from the 19:51 run (its 0 s trial
  file was overwritten by a later run).

### Run 2: 0 s gap, 10 trials, shell-based switch — [run2_gap0_x10_shell_switch/](run2_gap0_x10_shell_switch/)

The robot still switched links with shell commands, so it was offline
85–275 ms. Duplicate-address flags were cleared between trials.

| (min / median / max) | |
|---|---|
| Robot offline | 85 / 189 / 275 ms |
| leaf2 learns the robot MAC | 112 / 232 / 368 ms |
| Outage | 96 / 218 / 346 ms |
| Instructions missed | 1 / 2 / 3 |
| Counted as a move | 4 of 10 |

### Run 3: 0 s gap, 10 trials, instant switch — [run3_gap0_x10_instant_switch/](run3_gap0_x10_instant_switch/)

A switch agent already running on the robot does the whole switch in one
`ip -batch` call and sends the ARP itself. Duplicate-address flags were **not**
cleared between trials.

| (min / median / max) | |
|---|---|
| Robot offline | 15 / 67 / 102 ms |
| leaf2 learns the robot MAC | 12 / 66 / 99 ms |
| leaf1's withdraw removes the /32 at leaf5 | 198 / 315 / 429 ms |
| Outage | 11 / 34 / 191 ms |
| Instructions missed | 0 / 0 / 1 |
| Counted as a move | 10 of 10 (sequence 1, 3, 5 … 19) |
| Duplicate flagged | 8 of 10 trials |

Sequence numbers go up by 2 per trial because returning the robot to leaf1
between trials is also a move.

## Files in runs 2 and 3

| File | What it is |
|---|---|
| `summary.txt` | **Start here.** One row per trial, then min / median / max of every measurement. |
| `trial<N>_gap0.0s.txt` | One trial: timeline (ms after the robot dropped leaf1), outage, EVPN sequence numbers and duplicate flags on every leaf, missed instructions, and the raw kernel events. |
| `results.json` | All measurements in machine-readable form. |
| `console.log` | The test's console output. |
| `leaf<N>_ip.log`, `leaf<N>_fdb.log` | Raw `ip -ts monitor neigh route` and `bridge -timestamp monitor fdb` output from each leaf for the whole run. The timelines are built from these. |

All times are kernel timestamps from the leaves or `time.time()` on the
controller and robot; they share one clock because every node runs on the
same kernel.

## Runs left out

- **First run (19:00):** every trial showed 100% loss because the robot's
  echo responder crashed during the gap (a test bug, fixed before the 19:03
  run).
- **Run at 19:15–19:43:** the VM ran out of memory and swapped, so a 3 s gap
  took 48 s and the run took 28 minutes. Its timings are not valid.
- **First instant-switch run (20:21):** two analysis bugs (leaf2's MAC time
  and probes still in flight at the end of a trial); fixed and rerun as run 3.

## Caveats

- One lab VM running 9 routers in network namespaces; absolute times depend
  on how busy the VM is.
- The robot is a Linux host; a real robot's Wi-Fi roam and ARP behaviour may
  differ.

## Reproducing

```bash
docker exec -it frr-dev bash -c 'cd /home/frr/frr/tests/topotests && sudo -E python3 -m pytest -s bgp_evpn_capstone/test_evpn_capstone_break_before_make.py'
```

The evidence is written to
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone_break_before_make/break_before_make/`
in the container. Use `BBM_GAPS=0,0.5,1,3` and `BBM_REPEATS=1` for run 1's
setup.
