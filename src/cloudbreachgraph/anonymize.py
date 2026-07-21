"""``cloudbreachgraph-anonymize`` — scrub a ``graph.json`` into a shareable example.

An auxiliary CLI (separate console entry point from the main ``cloudbreachgraph``) that takes
a graph already written by a previous run — ``graph.json`` — and emits an anonymised copy that
keeps **every node and every relationship** but replaces all identifying values (IDs, ARNs,
IPs/CIDRs, DNS names, account ids, regions/AZs, security-group ids, opaque hashes, and human
names/labels) with random, format-preserving stand-ins. The result is safe to hand out as a
debugging/example graph.

The one hard guarantee is **referential consistency**: a given source value maps to exactly one
replacement *everywhere it appears*, including where it is embedded inside another string. If the
private IP ``10.0.1.20`` becomes ``10.44.2.187`` then every occurrence changes; if the ALB name
``my-alb`` becomes ``brisk-otter-7`` then its ARN node id, the edge that targets it, and the ENI
``Description`` token ``app/my-alb/…`` all change in lockstep.

How that guarantee is met (see ``docs/02_architecture.md §8``):

1. **Detect** sensitive literal tokens by scanning every string *value* in the graph with a
   small ordered battery of regexes (CIDR, IPv4, resource id, 12-digit account, AZ, region, long
   hex hash, long digit run). Overlapping matches are resolved longest/most-specific first so an
   id's hex suffix is never also captured as a bare hash.
2. **Names** (node labels / name-based ids like a Classic-ELB name) are whatever ``id``/``label``
   strings carry *no* pattern match at all — so an ARN (which does match) is anonymised by its
   parts, while a clean ``my-alb`` is anonymised as a whole.
3. **Map** each distinct token to a random, injective, format-preserving replacement (seeded via
   ``--seed`` for reproducibility). Injective so two distinct nodes never collapse into one.
4. **Rewrite** every string value in a **single left-to-right pass** using one alternation regex
   built from the map (longest token first). A single pass means a freshly-inserted value can
   never be re-matched and re-scrambled — which is what keeps embedded references consistent.

Dict *keys* and non-string scalars (bools, numbers, null) are left untouched, so structural
vocabulary (node ``type``, edge ``relationship``, attribute key names, the closed ``match_rule``
set) survives verbatim. Purely local file I/O; it never touches AWS.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .graph_io import GraphLoadError, graph_from_dict
from .output import json_export

# --------------------------------------------------------------------------- #
# Pools for format-preserving replacements
# --------------------------------------------------------------------------- #
# A spread of real AWS regions so a scrubbed region/AZ still looks plausible.
_AWS_REGIONS: tuple[str, ...] = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-north-1",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-northeast-2",
    "sa-east-1",
)
# Readable two-part names for labels (subnet/VPC/instance/LB names).
_NAME_ADJ: tuple[str, ...] = (
    "brisk", "calm", "dusky", "eager", "fizzy", "glossy", "hazy", "ivory", "jolly", "keen",
    "lush", "misty", "noble", "opal", "perky", "quiet", "rosy", "sable", "teal", "umber",
    "vivid", "wispy", "amber", "coral",
)  # fmt: skip
_NAME_NOUN: tuple[str, ...] = (
    "otter", "falcon", "cedar", "harbor", "meadow", "quartz", "comet", "willow", "ember",
    "pippin", "marlin", "cobalt", "zephyr", "juniper", "onyx", "larch", "raven", "thistle",
    "cypress", "plover",
)  # fmt: skip

# AWS resource-id prefixes we anonymise (kept as-is; only the suffix is scrambled). Longest
# first so the alternation prefers e.g. ``eipalloc`` over a shorter accidental match.
_ID_PREFIXES: tuple[str, ...] = (
    "eipalloc", "eni-attach", "subnet", "igw", "vgw", "tgw", "pcx", "rtb", "acl", "nacl",
    "dopt", "snap", "vpc", "eni", "sg", "nat", "ami", "vol", "lgw", "fl", "i",
)  # fmt: skip

# --------------------------------------------------------------------------- #
# Detection patterns, applied to every string VALUE. Ordered most-specific first so
# span-consumption keeps a longer match (a CIDR, a whole resource id) from being re-split into a
# shorter one (a bare IP, a hash) inside the same string.
# --------------------------------------------------------------------------- #
_PREFIX_ALT = "|".join(sorted(_ID_PREFIXES, key=len, reverse=True))
_ORDERED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cidr", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # prefix-suffix; suffix must contain at least one digit so hyphenated words ("nat-gateway")
    # aren't mistaken for ids while the hand-crafted "eni-00alb…" style still matches.
    ("resource", re.compile(rf"\b(?:{_PREFIX_ALT})-(?=[0-9a-z]*[0-9])[0-9a-z]{{3,}}\b")),
    ("account", re.compile(r"\b\d{12}\b")),
    ("az", re.compile(r"\b[a-z]{2}-[a-z]+-\d[a-z]\b")),
    ("region", re.compile(r"\b[a-z]{2}-[a-z]+-\d\b")),
    ("hex", re.compile(r"\b[0-9a-f]{16,}\b")),
    ("num", re.compile(r"\b\d{6,11}\b")),
)
_AZ_SPLIT = re.compile(r"^(.*\d)([a-z]+)$")  # region part + trailing AZ letter(s)


def _region_of_az(az: str) -> str:
    """``us-east-1a`` -> ``us-east-1`` (strip the trailing AZ letter)."""
    m = _AZ_SPLIT.match(az)
    return m.group(1) if m else az


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    """True if ``span`` intersects any already-consumed span in the same string."""
    return any(span[0] < end and start < span[1] for start, end in taken)


# --------------------------------------------------------------------------- #
# The anonymiser
# --------------------------------------------------------------------------- #
class Anonymizer:
    """Builds a consistent token→replacement map for one graph and applies it.

    Deterministic for a given ``seed``: token discovery and replacement generation both iterate
    in sorted order, so the same input + seed yields byte-identical output.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._map: dict[str, str] = {}  # source literal -> replacement
        self._region_map: dict[str, str] = {}  # region -> new region (shared by AZs)
        self._used: set[str] = set()  # every replacement handed out (keeps them injective)
        self._matched: set[str] = set()  # value strings that had >=1 pattern match

    # -- public API -------------------------------------------------------- #
    def anonymize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return an anonymised deep copy of a ``Graph.to_dict()`` mapping."""
        strings = sorted(set(_iter_value_strings(data)))
        self._detect_patterns(strings)
        self._detect_names(data)
        replace = _build_replacer(self._map)
        return _apply(data, replace)

    @property
    def mapping(self) -> dict[str, str]:
        """The final source→replacement map (for tests / a future ``--dump-map``)."""
        return dict(self._map)

    # -- detection --------------------------------------------------------- #
    def _detect_patterns(self, strings: list[str]) -> None:
        found: dict[str, set[str]] = {cat: set() for cat, _ in _ORDERED_PATTERNS}
        az_region: dict[str, str] = {}
        for s in strings:
            taken: list[tuple[int, int]] = []
            for cat, pattern in _ORDERED_PATTERNS:
                for m in pattern.finditer(s):
                    if _overlaps(m.span(), taken):
                        continue
                    taken.append(m.span())
                    tok = m.group(0)
                    found[cat].add(tok)
                    if cat == "az":
                        region = _region_of_az(tok)
                        az_region[tok] = region
                        found["region"].add(region)
            if taken:
                self._matched.add(s)

        # Regions first: AZs reuse the region map so an AZ and a bare region stay consistent.
        for region in sorted(found["region"]):
            self._region_map[region] = self._new_region(region)
            self._map[region] = self._region_map[region]
        for az in sorted(found["az"]):
            region = az_region.get(az, _region_of_az(az))
            self._map[az] = self._region_map[region] + az[len(region) :]
        for tok in sorted(found["cidr"]):
            self._map[tok] = self._new_cidr(tok)
        for tok in sorted(found["ipv4"]):
            self._map[tok] = self._new_ip(tok)
        for tok in sorted(found["resource"]):
            self._map[tok] = self._new_resource(tok)
        for tok in sorted(found["account"]):
            self._map[tok] = self._new_digits(12, first_nonzero=True)
        for tok in sorted(found["hex"]):
            self._map[tok] = self._new_hex(len(tok))
        for tok in sorted(found["num"]):
            self._map[tok] = self._new_digits(len(tok))

    def _detect_names(self, data: dict[str, Any]) -> None:
        """Map node ids/labels that carry no pattern match — human names, name-based ids.

        A string that *did* match a pattern (an ARN, a resource id) is anonymised by its parts
        elsewhere, so we must not also replace it wholesale here.
        """
        for node in data.get("nodes", []):
            if not isinstance(node, dict):
                continue
            for key in ("id", "label"):
                val = node.get(key)
                if (
                    isinstance(val, str)
                    and val
                    and val not in self._matched
                    and val not in self._map
                ):
                    self._map[val] = self._new_name()

    # -- replacement generators (all injective via self._used) ------------- #
    def _claim(self, value: str) -> str:
        self._used.add(value)
        return value

    def _new_region(self, original: str) -> str:
        for cand in self._rng.sample(_AWS_REGIONS, len(_AWS_REGIONS)):
            if cand != original and cand not in self._used:
                return self._claim(cand)
        # Exhausted the pool (many regions): synthesise a unique plausible one.
        while True:
            cand = f"xx-zone-{self._rng.randint(1, 99)}"
            if cand not in self._used:
                return self._claim(cand)

    def _new_name(self) -> str:
        while True:
            cand = (
                f"{self._rng.choice(_NAME_ADJ)}-{self._rng.choice(_NAME_NOUN)}-"
                f"{self._rng.randint(1, 99)}"
            )
            if cand not in self._used:
                return self._claim(cand)

    def _new_digits(self, length: int, *, first_nonzero: bool = False) -> str:
        while True:
            digits = [str(self._rng.randint(1 if first_nonzero else 0, 9))]
            digits += [str(self._rng.randint(0, 9)) for _ in range(length - 1)]
            cand = "".join(digits)
            if cand not in self._used:
                return self._claim(cand)

    def _new_hex(self, length: int) -> str:
        while True:
            cand = "".join(self._rng.choice("0123456789abcdef") for _ in range(length))
            if cand not in self._used:
                return self._claim(cand)

    def _new_resource(self, token: str) -> str:
        prefix, _, suffix = token.rpartition("-")
        while True:
            cand = f"{prefix}-{self._new_hex(len(suffix))}"
            if cand not in self._used:
                return self._claim(cand)

    def _new_ip(self, ip: str) -> str:
        while True:
            cand = str(ipaddress.ip_address(self._rand_ip_int(ip)))
            if cand not in self._used:
                return self._claim(cand)

    def _new_cidr(self, cidr: str) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        plen = net.prefixlen
        mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        base = str(net.network_address)
        while True:
            network = self._rand_ip_int(base) & mask
            cand = f"{ipaddress.ip_address(network)}/{plen}"
            if cand not in self._used:
                return self._claim(cand)

    def _rand_ip_int(self, sample: str) -> int:
        """A random 32-bit IPv4 int in the same class (private block / public) as ``sample``."""
        octets = [int(x) for x in sample.split(".")]
        r = self._rng.randint
        if octets[0] == 10:
            new = [10, r(0, 255), r(0, 255), r(1, 254)]
        elif octets[0] == 172 and 16 <= octets[1] <= 31:
            new = [172, r(16, 31), r(0, 255), r(1, 254)]
        elif octets[0] == 192 and octets[1] == 168:
            new = [192, 168, r(0, 255), r(1, 254)]
        else:  # public: avoid private/reserved first octets
            first = r(1, 223)
            while first in (10, 127, 169, 172, 192):
                first = r(1, 223)
            new = [first, r(0, 255), r(0, 255), r(1, 254)]
        return (new[0] << 24) | (new[1] << 16) | (new[2] << 8) | new[3]


# --------------------------------------------------------------------------- #
# Structure walking + single-pass replacement
# --------------------------------------------------------------------------- #
def _iter_value_strings(obj: Any) -> Any:
    """Yield every string *value* (never a dict key) reachable in ``obj``."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_value_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_value_strings(v)


def _apply(obj: Any, replace: Callable[[str], str]) -> Any:
    """Deep-copy ``obj`` applying ``replace`` to every string value; keys/scalars untouched."""
    if isinstance(obj, str):
        return replace(obj)
    if isinstance(obj, dict):
        return {k: _apply(v, replace) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_apply(v, replace) for v in obj]
    return obj


def _build_replacer(mapping: dict[str, str]) -> Callable[[str], str]:
    """One left-to-right pass replacing any mapped token (longest first) via ``mapping``.

    Single-pass alternation is the crux: each character is consumed once, so a value we just
    substituted in can never be re-matched against another token — that is what keeps embedded
    references (an IP inside a CIDR-free string, a name inside an ARN) consistent.
    """
    if not mapping:
        return lambda s: s
    tokens = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(t) for t in tokens))
    return lambda s: pattern.sub(lambda m: mapping[m.group(0)], s)


def anonymize_graph(data: dict[str, Any], seed: int | None = None) -> dict[str, Any]:
    """Anonymise a ``Graph.to_dict()`` mapping; convenience wrapper over :class:`Anonymizer`."""
    return Anonymizer(seed).anonymize(data)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_DEFAULT_OUTPUT = "anonymised_graph.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloudbreachgraph-anonymize",
        description="Anonymise a CloudBreachGraph graph.json: keep every node and relationship "
        "but randomise all identifiers, labels and attributes (consistently) so it can be "
        "shared as an example/debugging graph. Local only — no AWS calls.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("input", metavar="FILE", help="path to a graph.json file")
    p.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help=f"output path (default: {_DEFAULT_OUTPUT} beside the input)",
    )
    p.add_argument(
        "--seed",
        type=int,
        metavar="N",
        help="seed the randomisation for a reproducible result (default: random each run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path.parent / _DEFAULT_OUTPUT

    try:
        data = json.loads(in_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"cloudbreachgraph-anonymize: file not found: {in_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"cloudbreachgraph-anonymize: invalid JSON in {in_path}: {exc}", file=sys.stderr)
        return 2

    try:
        graph_from_dict(data)  # validate the input really is a graph before scrubbing
        anonymized = anonymize_graph(data, seed=args.seed)
        # Round-trip through the model so output is sorted/deterministic like every other writer.
        graph = graph_from_dict(anonymized)
    except GraphLoadError as exc:
        print(f"cloudbreachgraph-anonymize: {exc}", file=sys.stderr)
        return 2

    written = json_export.write_json(graph, out_path)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
