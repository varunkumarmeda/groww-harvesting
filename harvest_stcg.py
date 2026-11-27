"""Deprecated entry point. Use harvest.py; this forwards to it as short-term stocks only."""

import sys

from harvest import main

if __name__ == "__main__":
    print("harvest_stcg.py is deprecated; running: harvest.py --asset stocks --term st ...\n")
    sys.exit(main(["--asset", "stocks", "--term", "st", *sys.argv[1:]]))
