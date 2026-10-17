"""Package entrypoint for `python -m g15`"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
