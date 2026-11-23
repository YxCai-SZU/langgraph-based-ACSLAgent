"""Install the LLVM 12 runtime that the original veri-clang needs.

Run this once on every machine, from the project root:

    python scripts/setup_native.py

Inside WSL or Linux every command runs directly. Started from Windows Python,
the same commands are forwarded through `wsl.exe -d <distribution>`.

The script writes `.tools/native.json`. All paths in it are absolute Linux
paths for *this* machine, so copying the file to another machine does not
work; run the script again there instead.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT / ".tools"
VENDOR = PROJECT / "vendor/acslagent/LLM4Veri/llvm"

LLVM_NAME = "clang+llvm-12.0.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz"
LLVM_URL = f"https://github.com/llvm/llvm-project/releases/download/llvmorg-12.0.1/{LLVM_NAME}"
LLVM_SHA256 = "6b3cc55d3ef413be79785c4dc02828ab3bd6b887872b143e3091692fc6acefe7"
TINFO_NAME = "libtinfo5_6.1-1ubuntu1.18.04.1_amd64.deb"
TINFO_URL = f"https://archive.ubuntu.com/ubuntu/pool/main/n/ncurses/{TINFO_NAME}"
TINFO_SHA256 = "f7a59966bba0f997dd1bee0994c43cd563026a81e791cacc6540a2a8c9a49e53"

ON_WINDOWS = sys.platform == "win32"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def download(url: str, target: Path, expected: str) -> dict:
    if not target.exists():
        partial = target.with_suffix(".part")
        print(f"Downloading {target.name}", flush=True)
        with urllib.request.urlopen(url, timeout=90) as source, partial.open("wb") as dest:
            while block := source.read(4 * 1024 * 1024):
                dest.write(block)
        partial.replace(target)
    actual = sha256_file(target)
    if actual != expected:
        raise SystemExit(f"checksum mismatch for {target}; delete it and run again")
    return {"url": url, "file": str(target), "sha256": actual}


class Shell:
    """Run Linux commands directly, or through wsl.exe when on Windows."""

    def __init__(self, distribution: str):
        self.prefix = ["wsl.exe", "-d", distribution, "--exec"] if ON_WINDOWS else []

    def path(self, path: Path) -> str:
        if not ON_WINDOWS:
            return str(path.resolve())
        return self.output(["wslpath", "-a", str(path.resolve())]).strip()

    def run(self, command: list[str]) -> None:
        print("$ " + " ".join(command), flush=True)
        subprocess.run(self.prefix + command, check=True)

    def output(self, command: list[str]) -> str:
        data = subprocess.check_output(self.prefix + command)
        return data.decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--distribution", default="Ubuntu", help="WSL distribution (Windows only)")
    args = parser.parse_args()
    shell = Shell(args.distribution)

    downloads = TOOLS / "downloads"
    llvm = TOOLS / "llvm12"
    compat = TOOLS / "compat"
    for directory in (downloads, llvm, compat):
        directory.mkdir(parents=True, exist_ok=True)

    archives = [
        download(LLVM_URL, downloads / LLVM_NAME, LLVM_SHA256),
        download(TINFO_URL, downloads / TINFO_NAME, TINFO_SHA256),
    ]

    # 1. clang 12 and its builtin headers, which veri-clang calls internally.
    if not (llvm / "bin/clang-12").exists():
        member_root = "clang+llvm-12.0.1-x86_64-linux-gnu-ubuntu-"
        print("Extracting clang 12 (one pass over the archive, takes a minute)", flush=True)
        shell.run([
            "tar", "-xJf", shell.path(downloads / LLVM_NAME),
            "-C", shell.path(llvm), "--strip-components=1",
            member_root + "/bin/clang", member_root + "/bin/clang-12", member_root + "/lib/clang",
        ])

    # 2. libtinfo.so.5, which clang 12 links against and new Ubuntu lacks.
    shell.run(["dpkg-deb", "-x", shell.path(downloads / TINFO_NAME), shell.path(compat)])

    # 3. The vendored, unmodified original analyzer.
    manifest = json.loads((PROJECT / "vendor/acslagent/manifest.json").read_text(encoding="utf-8-sig"))
    for entry in manifest["files"]:
        if sha256_file(PROJECT / entry["vendored_file"]) != entry["sha256"]:
            raise SystemExit(f"vendored file changed: {entry['vendored_file']}")
    shell.run(["chmod", "+x", shell.path(VENDOR / "bin/veri-clang"), shell.path(llvm / "bin/clang-12")])

    llvm_path = shell.path(llvm)
    library_path = shell.path(compat) + "/lib/x86_64-linux-gnu"
    version = shell.output(["env", f"LD_LIBRARY_PATH={library_path}", llvm_path + "/bin/clang", "--version"])

    config = {
        "distribution": args.distribution,
        "compiler_directory": llvm_path + "/bin",
        "analyzer_root": shell.path(VENDOR),
        "library_path": library_path,
        "timeout_seconds": 30,
        "compiler_version": version.strip(),
        "tool_sha256": {
            "clang-12": sha256_file(llvm / "bin/clang-12"),
            "veri-clang": sha256_file(VENDOR / "bin/veri-clang"),
            "veri-llvm-pass.so": sha256_file(VENDOR / "lib/veri-llvm-pass.so"),
        },
    }
    write_json(TOOLS / "native.json", config)
    write_json(TOOLS / "installation.json", {"downloads": archives, "configuration": config})
    print(version, flush=True)
    print(f"Saved {TOOLS / 'native.json'}", flush=True)


if __name__ == "__main__":
    main()
