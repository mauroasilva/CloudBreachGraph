"""CloudBreachGraph — map an AWS account's network topology via the AWS CLI.

Phase 1 provides the foundation and the data-collection layer:

* :mod:`cloudbreachgraph.aws.runner`     — subprocess wrapper around ``aws``.
* :mod:`cloudbreachgraph.aws.collectors` — role-agnostic collectors + the role registry.
* :mod:`cloudbreachgraph.config`         — account/target resolution (account -> profile).

Phases 2 and 3 add the ``model``, ``mapping`` and ``output`` packages.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
