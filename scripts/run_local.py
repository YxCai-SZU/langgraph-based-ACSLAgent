"""Compatibility runner; the installed acslagent command uses the same CLI."""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))

if __name__ == '__main__':
    from acslagent.cli import main
    raise SystemExit(main())
