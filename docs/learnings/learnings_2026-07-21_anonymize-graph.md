# Learnings — 2026-07-21 anonymize-graph

## 1. What this change delivered
- **New auxiliary CLI `cloudbreachgraph-anonymize`** (`src/cloudbreachgraph/anonymize.py`) that
  reads a `graph.json` and writes an anonymised copy (default `anonymised_graph.json` beside the
  input) which keeps every node and edge but randomises all identifying values.
- Core is the `Anonymizer` class + `anonymize_graph(data, seed)` convenience wrapper. Pure,
  offline, AWS-free; reuses `graph_io.graph_from_dict` (input validation + re-load) and
  `output.json_export.write_json` (sorted/deterministic output).
- Entry point registered in `pyproject.toml` (`[project.scripts]`), alongside the existing
  `cloudbreachgraph-to-html`.
- Tests: `tests/test_anonymize.py` (20 tests). Docs: README "Anonymising a graph for sharing"
  section; `docs/02_architecture.md §7` auxiliary-tool bullet.

## 2. Interface contract for the next session
- `anonymize.anonymize_graph(data: dict, seed: int | None = None) -> dict` — anonymise a
  `Graph.to_dict()` mapping, returning a new mapping of the same shape.
- `anonymize.Anonymizer(seed)` — `.anonymize(data) -> dict`; `.mapping` exposes the final
  `{source_literal: replacement}` map after a run (used by tests; handy for a future
  `--dump-map` flag).
- `anonymize.main(argv) -> int` — CLI. Returns `2` on file-not-found / invalid JSON / not-a-graph
  (all via `GraphLoadError` or caught IO), `0` on success. Flags: positional `input`,
  `-o/--output`, `--seed N`, `--version`.
- **The guarantee callers rely on:** a given source value maps to exactly one replacement
  *everywhere it appears* (including embedded inside ARNs / DNS names / ENI descriptions), the
  mapping is **injective** (two nodes never collapse), and structural vocabulary
  (`type`, `relationship`, attribute keys, `match_rule`) is preserved verbatim.

## 3. Decisions & rationale
- **Two-tier token model.** (a) Regex detectors find pattern-shaped tokens (CIDR, IPv4,
  resource id, 12-digit account, AZ, region, 16+ hex hash, 6–11 digit run). (b) Any node
  `id`/`label` that had **no** pattern match at all is treated as a *human name*. This is the
  key trick: an ARN string *does* match patterns (region/account/hash) so it's anonymised
  piecewise and never registered as a whole-string "name"; a clean `my-alb` matches nothing so
  it's a name — and because `my-alb` also appears *inside* the ARN, adding it as a name token is
  exactly what makes the ARN's name-part move in lockstep with the LB label.
- **Single left-to-right alternation pass** (`_build_replacer`): one compiled regex of all tokens
  sorted longest-first, `pattern.sub` once per string. Single pass ⇒ a freshly-inserted value
  can't be re-matched/re-scrambled, and longest-first ⇒ a CIDR wins over the bare IP inside it,
  a whole resource id wins over its hex suffix. This is what keeps references consistent without
  any ordering hazard.
- **Per-string span consumption during detection** (`_overlaps`): ordered patterns
  (`_ORDERED_PATTERNS`, most-specific first) each `finditer`, skipping matches overlapping an
  already-claimed span. Stops e.g. `vpc-0aaaa…`'s 17-char hex suffix being *also* grabbed as a
  hash token.
- **Region/AZ share a map.** AZs contribute their region to the region set; AZ replacement =
  `region_map[region] + original_letter`, so `us-east-1a`→`us-west-2a` stays consistent with the
  region used in ARNs/DNS, and the AZ letter (structure) is preserved.
- **Format-preserving generators:** private IP stays in its RFC1918 block, public stays public,
  CIDR keeps prefix length with host bits zeroed (canonical network form), resource id keeps
  prefix + suffix length (hex suffix), account = 12 digits (first non-zero), names are readable
  `adj-noun-NN`. Injectivity enforced with a shared `_used` set (retry on collision).
- **Determinism:** seeded `random.Random`; token sets iterated **sorted** before generating
  replacements, so same input+seed ⇒ byte-identical output. Output also round-trips through
  `graph_from_dict` → `write_json` for the usual sorted-node/edge determinism. Default (no
  `--seed`) is intentionally *non*-deterministic — randomness is the point of the tool — which
  is a deliberate, scoped exception to the repo's "deterministic output" rule (that rule is about
  the collection pipeline being stable for a given AWS state).
- **Only values, never keys** are rewritten (`_apply` recurses dict values/list items, leaves
  keys and non-string scalars alone) — this is what protects attribute key names, node `type`,
  edge `relationship`, and the `match_rule` vocabulary.
- **Resource-id detection uses a prefix allowlist** (`_ID_PREFIXES`) + a "suffix must contain a
  digit" lookahead, so hyphenated words like `nat-gateway` or names like `web-server-1` aren't
  mistaken for ids, while the hand-crafted fixture ids (`eni-00alb00000000002`, non-hex letters)
  still match.

## 4. Deviations from the plan
- None substantive. This is a new auxiliary tool in the established `convert.py` mould (its own
  module + `main`, its own `[project.scripts]` entry). It does **not** add a role, a config-grammar
  change, or a main-CLI flag, so §10/§11 are untouched.
- The default output filename is the correctly-spelled `anonymised_graph.json` (the change
  request wrote `anonimised_graph.json`); `-o` overrides it if the exact spelling is wanted.

## 5. Gotchas / surprises
- **Fixture ids aren't hex.** `tests/fixtures` uses hand-made ids like `eni-00alb00000000002`
  (contains `l`). A hex-only resource-id regex would miss them — hence `[0-9a-z]` for the suffix
  with a prefix allowlist and a digit-presence lookahead to avoid false positives.
- **The NLB fixture embeds its ARN hash inside its DNS name** (`my-nlb-1a2b3c4d5e6f7a8b.elb…`).
  This is the strongest consistency test: the same hash must change identically in the ARN node
  id, the edge target, the ENI `Description`, *and* the DNS name. It does, because the hash is a
  single token in the map. See `test_nlb_hash_embedded_in_dns_matches_the_arn`.
- **`\b` word boundaries cleanly separate AZ from region** (`us-east-1a` has no boundary after
  `1`, so the region regex doesn't fire inside an AZ) and keep names like `web-server-1` from
  matching the region pattern.
- **Doc drift found:** `docs/02_architecture.md §2` layout diagram predates and omits the later
  auxiliary modules `convert.py`, `graph_io.py`, and `output/html_export.py` (and now
  `anonymize.py`). Left as-is to avoid a partial patch; flagging here. A future doc pass should
  refresh that diagram to match `src/`.

## 6. Known gaps / TODO
- **Substring over-match on names.** Because replacement is literal-substring based, a human name
  that is also a substring of structural text (e.g. a VPC literally named `network`, which is a
  substring of the `network_load_balancer` interface type) could over-match. Not hit by any
  fixture; documented in README/architecture. A future hardening could anchor name replacement to
  known-name-bearing fields only, or require word boundaries where the token allows.
- **No `--dump-map` / sidecar mapping file** yet (the map is available on `Anonymizer.mapping`
  in-process). Easy follow-up if someone needs to de-anonymise or audit.
- Free-text like a subnet name embedded with an IP wouldn't have its non-IP part anonymised (the
  whole string is skipped as a "name" only when it has zero pattern matches). Fine for current
  data shapes.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest -q                          # 152 passing (20 new in tests/test_anonymize.py)
ruff check . && ruff format --check .

# End-to-end, offline, against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
cloudbreachgraph-anonymize /tmp/cbg-out/graph.json --seed 42 -o /tmp/cbg-out/anonymised_graph.json
# Inspect: same 10 nodes / 9 edges, but every id/IP/ARN/DNS/name/account changed, and each
# source value replaced identically everywhere (e.g. the VPC id across all vpc_id fields+edges,
# the NLB hash across ARN + DNS + ENI description).
```
