"""Allow `python -m one_setup_all_agents`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
