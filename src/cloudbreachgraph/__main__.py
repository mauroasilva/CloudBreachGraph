"""Enable ``python -m cloudbreachgraph``.

The CLI itself lands in Phase 3 (``cli.py``). Until then this entry point reports
that the interface is not yet wired, without importing a module that does not exist.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from .cli import main as cli_main  # noqa: PLC0415  (Phase 3 module)
    except ModuleNotFoundError:
        print(
            "cloudbreachgraph: the command-line interface is not available yet "
            "(built in Phase 3). The Phase 1 collection layer is importable as "
            "`cloudbreachgraph.aws.collectors` and `cloudbreachgraph.config`.",
            file=sys.stderr,
        )
        return 2
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
