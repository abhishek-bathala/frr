# RFC Sections Relevant to the Capstone

The proposal cites two RFCs: **RFC 7432** (BGP MPLS-Based EVPN) and
**RFC 9721** (Extended Mobility Procedures for EVPN-IRB). RFC 7432 was written
for MPLS. This lab runs EVPN over VXLAN with symmetric IRB, so three companion
RFCs are also worth knowing. They are listed at the end.

## RFC 7432: BGP MPLS-Based Ethernet VPN

### Most relevant

**§7.2 MAC/IP Advertisement Route (RT-2).** This is the route that represents
a robot. A leaf advertises "MAC X, optionally with IP Y, is reachable behind
me", with the leaf's loopback as next-hop. Every mobility event in this
project is one RT-2 withdrawn by the old leaf and a new RT-2 advertised by the
new leaf. The route chart's RT-2 metric counts these routes.

**§7.7 MAC Mobility Extended Community.** This is a BGP extended community
attached to an RT-2. It carries a **sequence number** and a **sticky/static
flag**. It is how leaves agree on which copy of a moved MAC is current.

**§9 Determining Reachability to Unicast MAC Addresses.**

- §9.1 *Local learning*: a leaf learns a MAC on an access port, either from
  data-plane frames or by snooping ARP. In the lab, this is why the tests ping
  the gateway: the ping makes the leaf see the host.
- §9.2 *Remote learning*: the leaf advertises the MAC as an RT-2, and other
  leaves install it as reachable over the tunnel to that leaf. In the lab,
  the VXLAN devices use `nolearning`, so remote MACs come only from BGP.

**§10 ARP and ND.** An RT-2 can carry the IP bound to the MAC. Leaves can
then answer ARP requests locally (proxy ARP / ARP suppression) instead of
flooding them across the fabric.

- §10.1 *Default Gateway*: covers gateway MACs. This lab instead uses an
  anycast gateway: the same IP and MAC on every leaf. A robot that moves
  never has to re-learn its gateway.

**§15 MAC Mobility. This is the core section for the project.**

- The first advertisement of a MAC has no sequence number, which counts as
  0.
- When a leaf learns locally a MAC that another leaf advertised, it
  advertises the MAC with **sequence = highest seen + 1**.
- A leaf that receives a higher sequence number for its own local MAC
  **withdraws** its route. If two sequence numbers are equal, the leaf with
  the lower IP wins.
- Implementations must handle sequence-number wraparound.

The mobility test checks this rule: `localSequence` on the new leaf must be
greater than on the old leaf.

**§15.1 MAC Duplication Issue. This is critical for warehouse scale.**

- If a leaf sees **N = 5 moves within M = 180 seconds** (the defaults), it
  declares the MAC a **duplicate**. It alerts the operator and stops sending
  and processing RT-2 routes for that MAC.
- FRR uses the same defaults (`EVPN_DAD_DEFAULT_MAX_MOVES 5` and
  `EVPN_DAD_DEFAULT_TIME 180` in `bgpd/bgp_evpn_private.h`). You can change
  them with `dup-addr-detection max-moves N time M` and
  `dup-addr-detection freeze …` under `address-family l2vpn evpn`. They are
  enforced in `zebra/zebra_evpn_mac.c` and `zebra/zebra_evpn_neigh.c`.
- **Why it matters:** a robot that moves "several times per minute" reaches
  5 moves in about 1–2 minutes. The fabric would then treat a legitimately
  mobile robot as a misconfiguration. This conflict between duplicate
  detection and frequent mobility is probably the clearest research problem
  in the project.

**§15.2 Sticky MAC Addresses.** A MAC advertised as static (sticky flag set,
sequence 0) must never move. A leaf that learns that MAC locally raises an
alarm. This is the opposite of what robots need, but it is relevant for fixed
infrastructure such as the controller.

**§17 Convergence.** This section covers recovery from link, PE and access
failures (BFD, mass withdrawal). It is useful background for defining
convergence-time metrics, though it focuses on failures rather than host
moves.

### Useful background

- **§4 Overview** and **§6 Ethernet Tag ID**: EVI, broadcast domain and
  service interface terminology. Each L2VNI here is roughly one EVI and one
  broadcast domain.
- **§7.3 Inclusive Multicast Ethernet Tag Route (RT-3)**: each leaf
  announces "I'm a VTEP for VNI X" so that others know where to replicate
  broadcast, unknown-unicast and multicast (BUM) traffic. This route builds
  the flood list used for ARP before a robot is learned.
- **§7.9 / §7.10 RD and Route Targets**: an RD makes each leaf's routes
  unique, and route targets control import and export. §7.10.1
  auto-derivation includes the ASN. That is why the leaves use explicit
  `route-target 1000:1000` and `500:500`: every leaf has a different ASN.
- **§11 / §12 / §16**: handling of BUM and unknown-unicast traffic (ingress
  replication). This is what carries traffic to a robot during the gap after
  a move and before its new RT-2 arrives.

### Mostly not relevant here

- **§5, §7.1, §7.4–7.6, §8 Multihoming** (Ethernet Segments, ESI, DF
  election, aliasing): every host in this lab is single-homed (ESI 0). It
  would only matter if a team modeled a robot attached to two access points
  at once as multihoming.
- **§13, §14, §18**: MPLS forwarding, load balancing and frame-ordering
  details.

## RFC 9721: Extended Mobility Procedures for EVPN-IRB

RFC 7432's mobility rules assume a host's MAC and IP **move together**.
RFC 9721 (April 2025) extends them for IRB fabrics like this one, where RT-2
routes carry MAC+IP bindings.

- **§3.2 Mobility scenarios.** Three cases:
  1. MAC and IP move together (the RFC 7432 baseline, and what the current
     test does).
  2. The same IP moves with a **new MAC**, for example a robot whose NIC was
     replaced or a re-imaged host.
  3. The same MAC moves with a **new IP**, for example after a DHCP change.
  Cases 2 and 3 break a single per-MAC sequence number.
- **§5 / §6 Sequence-number assignment.** MAC-IP routes inherit the
  sequence number of their parent MAC route. Siblings that share a MAC share
  its sequence number. §6.1–6.5 give per-event rules for local and remote
  learning.
- **§6.3 / §6.8 Stale entries and convergence.** After a move, the old leaf
  may still hold stale local MAC and ARP entries. The RFC describes
  **probing** them (ARP/ND) and deleting them, and optional probing for
  faster relearning. This directly affects time-to-reachability.
- **§7 Routed overlay.** Mobility for host /32 routes advertised as RT-5
  without MAC routes. This is an alternative design a mobility-improvement
  team could evaluate.
- **§8 Duplicate host detection.** Refines §15.1 of RFC 7432 for duplicate
  MACs, duplicate IPs with different MACs, and routed overlays, plus recovery
  by unfreezing. It is relevant to the duplicate-detection problem above.
- **§9 Security.** A compromised access port can fake mobility to steal
  traffic. This matters in a warehouse where any robot could act this way.

## Companion RFCs for the VXLAN / IRB parts of the lab

- **RFC 8365, EVPN as a Network Virtualization Overlay:** EVPN over VXLAN.
  The VNI replaces the MPLS label, and the VTEP loopback becomes the
  next-hop.
- **RFC 9135, Integrated Routing and Bridging in EVPN:** symmetric IRB, the
  L3VNI (VNI 500, VRF500) model. It is what lets the controller on L2VNI 2000
  reach robots on L2VNI 1000.
- **RFC 9136, IP Prefix Advertisement in EVPN:** the RT-5 route. The tests
  check that the SVI subnets are exchanged as RT-5 routes.
- **RFC 7938, Use of BGP for Routing in Large-Scale Data Centers:** the
  eBGP underlay design: one ASN per leaf, a shared spine ASN, unnumbered
  sessions.

## Mapping to project phases

| Phase | RFC material |
|---|---|
| 2: FRR familiarization | RFC 7432 §7.2, §7.3, §9; RFC 8365; RFC 9135 |
| 3: Metrics definition | §15 (sequence-number churn), §15.1 (duplicate freezes), §17 (convergence) |
| 4: Characterization | §15.1 limits vs. robot move rate; RFC 9721 §6.8 stale-entry delay |
| 5: Improvement path | RFC 9721 §5–§8 (sequence handling, probing, routed overlay, duplicate detection tuning) |
| 5: Monitoring path | Sequence numbers, duplicate-detection state, RT-2 add/withdraw rates per leaf |
