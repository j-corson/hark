"""hark: listen to a conversation and keep a live, structured document with Claude."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main
    _main()
