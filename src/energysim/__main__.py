"""Allow running the tool with `python -m energysim`."""

from energysim.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
