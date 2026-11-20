#!/usr/bin/env python3
"""Kwikset 913/914 bench harness -- thin shim over the kwikset_bench package.

The monolith this file used to be is now the `kwikset_bench` package next to
it (see PROTOCOL.md 15 for the layout). This shim exists so the original
invocation keeps working unchanged:

    ../.venv/bin/python kwikset_bench.py <mode> -p PORT ...
    ../.venv/bin/python -m kwikset_bench <mode> -p PORT ...   (same directory)
"""
from kwikset_bench import main

if __name__ == "__main__":
    main()