"""Fetch one official Blender build, check it against its pinned SHA-256, unpack it, and name its binary.

    python .github/scripts/fetch_blender.py --file blender-5.0.1-linux-x64.tar.xz --sha256 <64 hex> \\
        --cache <download folder> --dest <unpack folder>

The archive is downloaded from download.blender.org. If that site refuses, it is downloaded from
mirror.blender.org, Blender's own redirector to its mirrors, because download.blender.org sits behind a
Cloudflare challenge that can return 403 to clients that are not browsers. The pinned SHA-256, taken from the
release's own .sha256 file, is what makes either source trustworthy, so a cached archive is checked again on
every run. The binary's path is printed to stdout. On a GitHub runner it is also written to BLENDER_BIN through
GITHUB_ENV, which is how the integration tests find it.

The script uses the standard library only, because it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

SOURCES = ("https://download.blender.org/release", "https://mirror.blender.org/release")
USER_AGENT = "blenderlens-ci (+https://github.com/pzalutski-pixel/blenderlens-mcp)"
#: An official archive name, such as blender-5.0.1-linux-x64.tar.xz: the version, then its major and minor parts.
ARCHIVE_NAME = re.compile(r"blender-((\d+)\.(\d+)\.\d+)-")


def release_of(file: str) -> tuple[str, str]:
    """Return the version an archive name carries and its release folder, such as ("5.0.1", "Blender5.0").

    Exit with an error annotation when the name is not an official Blender archive name.
    """
    found = ARCHIVE_NAME.match(file)
    if not found:
        raise SystemExit(f"::error::{file} is not an official Blender archive name, such as "
                         "blender-5.0.1-linux-x64.tar.xz")
    return found.group(1), f"Blender{found.group(2)}.{found.group(3)}"


def sha256_of(path: Path) -> str:
    """Return the SHA-256 of a file's contents, as lowercase hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, attempts: int = 3) -> None:
    """Download ``url`` to ``target``, retrying a server error or a network failure up to ``attempts`` times."""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            return
        except urllib.error.HTTPError as exc:
            # A 403 or 404 will not change on a retry, but a 5xx error might.
            if exc.code < 500 or attempt == attempts:
                raise
        except OSError:
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def fetch(file: str, sha256: str, cache: Path) -> Path:
    """Return the verified archive in the cache folder, downloading it only when the cache has no good copy."""
    _, series = release_of(file)
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / file
    if archive.is_file():
        if sha256_of(archive) == sha256:
            print(f"Using the cached {file}", file=sys.stderr)
            return archive
        print(f"The cached {file} does not match its SHA-256; downloading it again", file=sys.stderr)
        archive.unlink()
    partial = archive.with_name(archive.name + ".part")
    failures = []
    for source in SOURCES:
        url = f"{source}/{series}/{file}"
        try:
            print(f"Downloading {url}", file=sys.stderr)
            download(url, partial)
        except OSError as exc:  # HTTPError and URLError are both subclasses of OSError.
            failures.append(f"{url}: {exc}")
            continue
        actual = sha256_of(partial)
        if actual != sha256:
            failures.append(f"{url}: SHA-256 {actual}, expected {sha256}")
            partial.unlink()
            continue
        partial.replace(archive)
        return archive
    raise SystemExit("::error::Could not fetch a verified " + file + ":\n" + "\n".join(failures))


def _long(path: Path) -> Path:
    """Return ``path`` in the form Windows accepts beyond 260 characters, or unchanged on other systems.

    Blender's bundled Python library goes past that length when it is unpacked under a deep folder.
    """
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path.resolve()))
    return path


def unpack(archive: Path, dest: Path) -> Path:
    """Unpack the archive into ``dest``, emptied first, and return the path of the Blender executable."""
    if dest.exists():
        shutil.rmtree(_long(dest))
    dest.mkdir(parents=True)
    name = archive.name
    if name.endswith(".tar.xz"):
        with tarfile.open(archive, "r:xz") as tar:
            # The "tar" filter keeps Blender's own symlinks and file modes, and refuses paths outside dest. A
            # Python without extraction filters extracts without one, and the archive's SHA-256 is checked either way.
            if hasattr(tarfile, "tar_filter"):
                tar.extractall(dest, filter="tar")
            else:
                tar.extractall(dest)
        return dest / name[: -len(".tar.xz")] / "blender"
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(_long(dest))
        return dest / name[: -len(".zip")] / "blender.exe"
    if name.endswith(".dmg"):
        mount = Path(tempfile.mkdtemp(prefix="blender-dmg-"))
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                        str(archive)], check=True, stdout=subprocess.DEVNULL)
        try:
            shutil.copytree(mount / "Blender.app", dest / "Blender.app", symlinks=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=False, stdout=subprocess.DEVNULL)
        return dest / "Blender.app" / "Contents" / "MacOS" / "Blender"
    raise SystemExit(f"::error::Do not know how to unpack {name}")


def main(argv: list[str] | None = None) -> int:
    """Fetch, check and unpack the archive named on the command line, then print and record its binary's path."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", required=True, help="the archive's name on download.blender.org")
    parser.add_argument("--sha256", required=True, help="the archive's SHA-256, from the release's .sha256 file")
    parser.add_argument("--cache", required=True, type=Path, help="where downloads are kept between runs")
    parser.add_argument("--dest", required=True, type=Path, help="where Blender is unpacked")
    args = parser.parse_args(argv)

    expected, _ = release_of(args.file)
    binary = unpack(fetch(args.file, args.sha256.lower(), args.cache), args.dest)
    if not binary.is_file():
        raise SystemExit(f"::error::{args.file} did not hold {binary}")
    run = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=300)
    first = (run.stdout.strip().splitlines() or [""])[0]
    if run.returncode != 0 or f"Blender {expected}" not in first:
        # On Linux this is where a missing system library shows, as "error while loading shared libraries".
        raise SystemExit(f"::error::{binary} --version exited with {run.returncode} and printed {first!r}\n"
                         f"{run.stderr[-3000:]}")
    print(f"{first} at {binary}", file=sys.stderr)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
            env.write(f"BLENDER_BIN={binary}\n")
    print(binary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
