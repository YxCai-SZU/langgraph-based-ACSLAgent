"""Register this Improve checkout as acslagent in the active Python environment.

Run after copying the whole project into WSL/Linux:
    conda activate acslagent
    python scripts/install.py

Reuses existing native tools and Python dependencies. Does not install/change
Frama-C, opam, Conda or API credentials. pip may fetch its isolated build backend.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import sysconfig
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-native', action='store_true',
                        help='keep an already configured veri-clang installation')
    args = parser.parse_args()
    if sys.platform == 'win32':
        parser.error('run this installer with Python inside WSL/Linux and the target Conda environment')
    print(f'Installing ACSLAgent Improve from {PROJECT}', flush=True)
    print(f'Python environment: {sys.prefix}', flush=True)
    if not args.skip_native:
        subprocess.run([sys.executable, str(PROJECT/'scripts/setup_native.py')], check=True)
    # Same distribution/command as the original, so installation explicitly
    # changes future acslagent invocations in THIS environment to this checkout.
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '-e', str(PROJECT)], check=True)
    executable = Path(sysconfig.get_path('scripts'))/'acslagent'
    subprocess.run([str(executable), '--version'], check=True)
    print(f'Ready: {executable}', flush=True)
    print('Now run: acslagent run <file.c> -m <model> --retries 2 --repairs 3', flush=True)


if __name__ == '__main__':
    main()
