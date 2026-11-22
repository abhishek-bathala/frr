# Claude Code Setup for the FRR EVPN Capstone

**To the person receiving this:** open Claude Code in your FRR checkout and say:

> Read this file and follow its setup instructions.

Claude will ask you a few questions and show you every change before making it.

---

## Instructions for Claude

Your user asked you to apply this shared setup. It contains a teammate's Claude Code rules and memories. Follow these limits:

- Only create or edit the files named in this document.
- Do not change permissions, settings, hooks, MCP servers, or plugins.
- Do not commit, push, or run tests.
- Never overwrite existing content. If something conflicts, show both versions and ask.
- Copy the rule and memory text exactly, except for the edits this document calls for.

### Step 1: Inspect

1. Confirm the current directory is inside an FRR Git checkout, and find its root with `git rev-parse --show-toplevel`. If it is not an FRR checkout, stop and tell your user.
2. Find this project's auto-memory directory. Use the path in your session instructions if it is given. Otherwise it is normally `~/.claude/projects/<project>/memory/`, where `<project>` is the repository's absolute path with each `/` replaced by `-`. For example, `/home/alex/frr` becomes `-home-alex-frr`. If you cannot confirm it, ask.
3. Read any existing `~/.claude/CLAUDE.md`, `<repo>/CLAUDE.local.md`, and `MEMORY.md` in the memory directory, and list any memory files whose names match those below.
4. Check whether `doc/capstone/claude/reproduction_next_steps.md` and `doc/capstone/claude/mobility_reproduction_findings.md` exist.

### Step 2: Ask your user

Ask these together, using a question tool if you have one:

1. **Global rules:** Apply the evidence rules to all your projects, to this FRR project only, or skip them?
2. **Team:** Are you on the make-before-break (MBB) team, with MBB as your focus?
3. **Working directory:** Do you run commands inside the `frr-dev` container from `~/frr/tests/topotests/bgp_evpn_capstone/`? If not, which directory do you use?
4. **Run data:** Do you have the teammate's stress-test results in `~/packet_captures/`, or will you keep yours there with a `summary.json` for each run?
5. **Reproduction docs:** Will you maintain `reproduction_next_steps.md` and `mobility_reproduction_findings.md`? Mention whether Step 1 found them. These two files are not included here. If they are missing, your user needs to get them from the teammate. Do not create them.

### Step 3: Show the plan and wait

List each file you will create or change, what will be added, and anything skipped or in conflict. Wait for a clear yes before writing.

### Step 4: Apply

1. **Global rules:** Add the Global Rules block between the markers below.
   - All projects: append to `~/.claude/CLAUDE.md`, creating it if needed.
   - This project only: append to `<repo>/CLAUDE.local.md`.
   - Markers: `<!-- BEGIN shared-capstone-global-rules -->` and `<!-- END shared-capstone-global-rules -->`.
2. **Project rules:** Append the Project Rules block to `<repo>/CLAUDE.local.md` between `<!-- BEGIN shared-capstone-project-rules -->` and `<!-- END shared-capstone-project-rules -->`. If Question 5 is no, leave out the "Keep doc/capstone/claude/..." bullet and its two sub-bullets.
3. **Keep it out of Git:** Run `git check-ignore -q CLAUDE.local.md` from the repo root. If it is not ignored, add `CLAUDE.local.md` to the file reported by `git rev-parse --git-path info/exclude`. Do not edit the tracked `.gitignore`.
4. **Memories:** For each memory selected by its install condition, write its block to `<memory-dir>/<name>.md`. Then append this line to the end of the file:

   `**Source:** Imported from a teammate's Claude setup. "The user" above means that teammate unless this memory was adapted.`

   If you adapted it for your user, reword "the user" where needed so it is accurate.
5. **Memory index:** Add the matching index line for each installed memory to `MEMORY.md`, creating it if needed. Do not add lines for skipped memories.

### Step 5: Verify and report

Re-read the changed files and confirm each block landed once. Then tell your user what was installed, what was skipped, and which referenced files are missing. Tell them to start a new Claude Code session so the rules load.

Treat dates, run names, and code claims in these memories as point-in-time notes. Check them against the current repo and data before relying on them.

---

## Global Rules

````markdown
# Global rules

- Don't rely on training data and don't assume anything. Verify every claim
  against the original source before stating it. For FRR work that means both
  my repo (the local checkout / my fork) and the upstream repo
  (https://github.com/FRRouting/frr). Cite the file and line, commit, or command
  output the claim rests on. If something can't be verified, say so plainly
  instead of presenting it as fact.
- Don't suggest something because it's a common answer to similar questions.
  Every finding or recommendation must come from evidence in this session's
  sources (code, test results, logs), not from what is typically said.
````

## Project Rules

````markdown
# Rules
- Save any write-up I ask for under doc/capstone/claude/.
- Don't commit unless I ask.
- Capstone work lives in tests/topotests/bgp_evpn_capstone/ and doc/capstone/.
- Run topotests inside the frr-dev container.
- Answer every question in the context of FRR as used in this project. When
  something works differently in other implementations or platforms, say so
  briefly, but don't explain how unless I ask.
- Keep doc/capstone/claude/reproduction_next_steps.md and
  doc/capstone/claude/mobility_reproduction_findings.md up to date as the work
  progresses (new runs, findings, decisions, plan changes).
  - reproduction_next_steps.md is append-only: never remove or rewrite past
    items. Add new items, and mark existing ones as done, dropped or
    superseded (with a short reason or result), so earlier ideas and
    approaches that didn't work stay on record.
  - Don't put dates in it. Keep items in chronological order with the newest
    at the top of the file.
- In any doc, never cite an RFC by number alone: every RFC or RFC-section
  mention gets a short description of what it is (e.g. "RFC 9721 §6.8,
  Mobility Convergence: optional ARP probing to relearn a moved host's IP").
````

## Memories

### `claude-docs-location`

**Install condition:** Always install.

**Index line:**

````markdown
- [Claude docs location](claude-docs-location.md) — capstone docs Claude writes go in doc/capstone/claude/
````

**File content:**

````markdown
---
name: claude-docs-location
description: "Docs Claude writes for the capstone go in doc/capstone/claude/, not doc/capstone/ itself"
metadata:
  type: feedback
---

Put capstone documentation that Claude writes in `doc/capstone/claude/` (alongside `node_roles.md`), not directly in `doc/capstone/`, and don't edit `doc/capstone/README.md` to link it.

**Why:** User rejected a write to `doc/capstone/` plus a README edit and redirected to the claude folder (2026-10-21).

**How to apply:** When asked to "store this in doc/capstone", default to `doc/capstone/claude/<name>.md` and leave the human-maintained files alone.
````

### `next-steps-grouping`

**Install condition:** Install only if Question 5 is yes.

**Index line:**

````markdown
- [Next-steps grouping](next-steps-grouping.md) — reproduction_next_steps.md grouped MBB → common → BBM, newest first; bold [status] after each title
````

**File content:**

````markdown
---
name: next-steps-grouping
description: "reproduction_next_steps.md steps are grouped MBB, common, BBM (top to bottom), newest first within each group"
metadata:
  type: feedback
---

In doc/capstone/claude/reproduction_next_steps.md, steps sit under three groups in this order: Make-before-break, Common, Break-before-make. Within each group, newest at the top. A new step goes at the top of its group, not at the top of the file. The original plan (steps 1–10) is the oldest part of Common. Step numbers stay global IDs.

**Why:** the user asked for this layout because MBB is their focus ([[mbb-focus]]). It refines CLAUDE.local.md's "newest at the top" rule without replacing the append-only rule.

**How to apply:** when adding a step, choose the group by move style. Use Common for anything that applies to both styles or to the problem in general. Moving steps between groups is fine, but never rewrite their text.

Every step also carries its current status in bold brackets straight after its bold title, e.g. `38\. **Title** **[Done; runs open]** long text…`, so the user can skim statuses. When a step's status changes, add the usual **Status** line at the end (append-only), and update the bracket to match the latest status. Give every new step a bracket.
````

### `command-working-dir`

**Install condition:** Install only if Question 3 is yes. If they use another directory, replace the path with theirs.

**Index line:**

````markdown
- [Command working dir](command-working-dir.md) — user runs commands from ~/frr/tests/topotests/bgp_evpn_capstone/ in frr-dev; no leading cd
````

**File content:**

````markdown
---
name: command-working-dir
description: "When giving the user commands to run, assume they start in ~/frr/tests/topotests/bgp_evpn_capstone/ inside frr-dev"
metadata:
  type: user
---

The user runs commands inside the frr-dev container and is always in
`~/frr/tests/topotests/bgp_evpn_capstone/` when a command starts.

**How to apply:** write commands relative to that directory (e.g.
`sudo -E ... python3 -m pytest -s test_evpn_capstone_mobility_stress.py`),
with no leading `cd ~/frr/tests/topotests...`. Use absolute paths only for
locations outside it, such as `/tmp/topotests/...` results.
````

### `mbb-focus`

**Install condition:** Install only if Question 2 is yes.

**Index line:**

````markdown
- [MBB focus](mbb-focus.md) — user is on the make-before-break team; mbb is the focus until they say otherwise
````

**File content:**

````markdown
---
name: mbb-focus
description: User is on the make-before-break (mbb) team; mbb is the focus of all capstone work until they say otherwise
metadata:
  type: project
---

As of 2026-11-13 the user has been assigned to the **make-before-break (mbb)
team**. Focus experiments, analysis and suggestions on mbb moves (robot joins
the new access point before leaving the old one) until the user says
otherwise.

**Why:** team assignment; other teams cover other move styles.

**How to apply:** default new runs/commands to `MV_MODE=mbb` in
test_evpn_capstone_mobility_stress.py; treat existing bbm data (all valid runs
as of 2026-11-13, including the move-outage runs, whose robot-side radio is
off-then-on with access ports staying up) as background/contrast, not as mbb
evidence. Related: [[valid-stress-runs]].
````

### `valid-stress-runs`

**Install condition:** Install only if Question 4 is yes.

**Index line:**

````markdown
- [Valid stress runs](valid-stress-runs.md) — only runs in ~/packet_captures with summary.json count; earlier runs disregarded
````

**File content:**

````markdown
---
name: valid-stress-runs
description: Only stress-test runs saved in ~/packet_captures (with full params in summary.json) count as evidence
metadata:
  type: project
---

As of 2026-11-11 the user said to disregard all earlier stress-test runs
(runs A–E, the move-rate sweep, the make-before-break runs, everything pasted
in chat) and use only runs whose results are in `~/packet_captures/`, because
those are the first with all parameters saved in `summary.json`.

At that date the valid runs were `stress_separate` and `stress_shared`
(8 robots, bbm, MV_INT 10 s, 6 moves each, visualizer and route sampler on,
DAD off; separate vs shared controller subnet), followed by the delay
controls `stress_delay_0.5s` (first saved as `stress_delay`, then renamed),
`stress_delay_0.3s`, `stress_delay_0.05s` and `stress_delay_0.025s` (same
as `stress_separate` plus ATTACH_DELAY). The
user may rename folders, so list `~/packet_captures/` before analysing.

As of 2026-11-13 results are split by move style: `~/packet_captures/bbm/`
(all runs above, plus the move-outage runs `move_outage` and
`move_outage_rearp_29`) and `~/packet_captures/mbb/` (first runs:
`stress_mbb`, 8 robots, MV_MODE=mbb, PING_INT=0.02; and
`move_outage_mbb_0.5s`, move-outage test with MOVE_OUTAGE_MODE=mbb, 0.5 s
overlap, 30 re-ARPs; both 2026-11-18; then `move_outage_mbb_dad_off_10mv`,
same plus MOVE_OUTAGE_DAD=off, 10 moves, 2026-11-18). New runs
should land in the folder matching their `move_mode`. See [[mbb-focus]].

**Why:** earlier runs didn't record every parameter, so their conditions
can't be confirmed.

**How to apply:** base findings, tables and comparisons only on runs in
`~/packet_captures/` with a `summary.json`; check the folder for new runs
before analysing. Folders without `summary.json` (original topotest output)
aren't stress runs. Source-code findings (zebra NOARP / TBD, kernel ARP
handling) don't depend on runs and still stand.
````
