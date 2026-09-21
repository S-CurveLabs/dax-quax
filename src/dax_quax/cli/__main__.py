"""Lets `python -m dax_quax.cli` work alongside the installed `dax-quax` script.

The guard matters more than it looks: without it, anything that *imports* this module runs
the CLI. pkgutil.walk_packages does exactly that, so the CI import check caught it.
"""

from dax_quax.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
