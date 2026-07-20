# 01 — Overview

## What we are building

**CloudBreachGraph** is a command-line Python application that inspects a single AWS
account (in one or more regions) and produces a **map** of how its network primitives
connect. It reads live data through the **AWS CLI** and outputs a graph that can be
serialized to JSON and rendered visually (Graphviz DOT, and optionally PNG/SVG).

The name reflects the intended use: understanding the network reachability surface of an
account for **defensive security review and asset inventory**. It is a read-only
inventory/mapping tool. It does not modify any AWS resources.

## The map

The graph connects five AWS resource types with directed edges:

```
                 ┌────────────────────┐
                 │  EC2 Instance      │◄─────┐  attached_to
                 └────────────────────┘      │
                                             │
 ┌───────────────────────┐   attached_to   ┌┴──────────────────────┐
 │  Network Interface    │─────────────────►│                       │
 │  (ENI)                │                  │  Load Balancer        │
 └───────────┬───────────┘   (one of        │  (ALB / NLB / Classic)│
             │                these two)     └───────────────────────┘
             │ in_subnet
             ▼
 ┌───────────────────────┐   in_vpc      ┌───────────────────────┐
 │  Subnet               │──────────────►│  VPC                  │
 └───────────────────────┘               └───────────────────────┘
```

**Traversal / build order (as requested):**

1. Enumerate **all Network Interfaces (ENIs)** — these are the anchor of the graph.
2. Connect each ENI to the **EC2 Instance** or **Load Balancer** it belongs to.
3. Map each ENI to its **Subnet**.
4. Connect each Subnet to its **VPC**.

Every ENI belongs to exactly one subnet, and every subnet belongs to exactly one VPC, so
those edges are always resolvable. The attachment edge (instance vs. load balancer vs.
neither) depends on what the ENI is used for — see `02_architecture.md` for the exact
resolution rules, which are the trickiest part of the whole app.

## Goals

- **G1** — Enumerate ENIs, EC2 instances, load balancers, subnets, and VPCs via the AWS CLI.
- **G2** — Correctly attribute each ENI to its owning EC2 instance or load balancer.
- **G3** — Correctly place each ENI in its subnet, and each subnet in its VPC.
- **G4** — Emit a graph as JSON and as Graphviz DOT (renderable to PNG/SVG if `dot` is installed).
- **G5** — Work against a real account with a single command, region-scoped, read-only.
- **G6** — Be testable offline using recorded AWS CLI JSON fixtures (no live account needed for CI).

## Non-goals (v1)

- No boto3 / AWS SDK. The user specifically wants the **AWS CLI** as the data source.
- No write operations, no remediation, no "breach simulation" — mapping only.
- No AWS Organizations auto-discovery. Accounts are declared by the operator in config.
- v1 collects only the **`network`** role (ENIs, EC2, LB, subnets, VPCs) and from a single
  account. **However**, the config and architecture are built multi-account-ready from day one:
  a "target" binds resource roles to accounts so a later feature (e.g. VPC flow logs living in a
  separate logging account) can pull different resource types from different accounts/profiles in
  one run — see `02_architecture.md §11` and `05_roadmap.md`. That cross-account collection is
  designed now but not implemented in v1.
- No other resource types (security groups, route tables, IGWs, NAT, RDS, flow logs, etc.) in v1.
  The architecture explicitly leaves room to add them as new roles later (see `05_roadmap.md`).
- No web server / live UI. Output is files (JSON + DOT, optional rendered image).

## Assumptions about the target account

- The operator uses **one named AWS CLI profile per account** and wants to select an account
  without remembering which profile it maps to. CloudBreachGraph accepts an account→profile
  mapping (config file) plus CLI flags so you can say "for account X, use profile Y" — see
  `02_architecture.md §10`. When a single environment's resources span accounts (e.g. flow logs
  in a logging account), a **target** binds each resource role to an account — see §11.
- Each profile has **read-only** permissions:
  `ec2:DescribeNetworkInterfaces`, `ec2:DescribeInstances`, `ec2:DescribeSubnets`,
  `ec2:DescribeVpcs`, `elasticloadbalancing:DescribeLoadBalancers`, and
  `sts:GetCallerIdentity` (for the account-verification check).
- The AWS CLI is invoked as a subprocess (`aws ...`) with `--output json`.
- Region is provided explicitly (default from the CLI config or per-account config, overridable via a flag).
- Accounts may be large: hundreds/thousands of ENIs. The AWS CLI auto-paginates, but the
  app must not assume tiny result sets.

## Primary user story

> "As a security engineer with a separate AWS CLI profile per account, I keep a small config
> file mapping each account to its profile. I run `cloudbreachgraph --account prod` (or
> `--account 111111111111`), the tool picks the `prod-audit` profile for me, verifies it really
> points at that account, and writes `graph.json` plus `graph.dot` showing every ENI, what
> compute or load balancer it belongs to, and which subnet and VPC it lives in — so I can see
> the network layout of the account at a glance. `--profile <name>` still works as a direct
> override when I don't want to touch the config."
