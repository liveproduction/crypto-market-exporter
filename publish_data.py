"""Publish the validated latest export as a single root commit on origin/data."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

from exporter import require, validate_export

DATA_REF = "refs/heads/data"
SNAPSHOT_PATH = re.compile(r"latest/(manifest\.json|(?:1d|4h)-part-[0-9]{3,}\.txt)\Z")


def git(repo, *args, content=None, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], input=content,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode('utf-8', errors='replace')}")
    return result.stdout


def snapshot_at(repo, commit):
    paths = git(repo, "ls-tree", "-r", "--name-only", commit).decode("utf-8").splitlines()
    require(paths and all(SNAPSHOT_PATH.fullmatch(path) for path in paths),
            "data branch contains files outside the managed latest snapshot")
    return {path: git(repo, "show", f"{commit}:{path}") for path in paths}


def materialize(files, directory):
    latest = directory / "latest"
    latest.mkdir(parents=True)
    for name, content in files.items():
        require(SNAPSHOT_PATH.fullmatch(name), "Unexpected snapshot path")
        (directory / name).write_bytes(content)
    return validate_export(latest)


def publish_data(latest, repo):
    repo = repo.resolve()
    validate_export(latest)
    files = {f"latest/{path.name}": path.read_bytes() for path in latest.iterdir()}
    # Freeze and revalidate the bytes that will actually be committed, not a live folder.
    with tempfile.TemporaryDirectory(prefix="data-snapshot-") as temporary:
        temporary = Path(temporary)
        manifest = materialize(files, temporary / "candidate")
        advertised = git(repo, "ls-remote", "--heads", "origin", DATA_REF).decode().splitlines()
        require(len(advertised) <= 1, "Ambiguous data branch")
        expected = ""
        if advertised:
            # The fetched tip is our comparison/lease target even if it changed since ls-remote.
            git(repo, "fetch", "--no-tags", "origin", DATA_REF)
            expected = git(repo, "rev-parse", "FETCH_HEAD").decode().strip()
            old_files = snapshot_at(repo, expected)
            previous = materialize(old_files, temporary / "previous")
            if old_files == files:
                print(f"data already contains this exact snapshot: {expected}")
                return expected
            validate_export(temporary / "candidate" / "latest", previous)

        # Separate index; never switch branches or alter the checkout's staged files.
        env = dict(os.environ, GIT_INDEX_FILE=str(temporary / "index"),
                   GIT_AUTHOR_NAME="github-actions[bot]", GIT_COMMITTER_NAME="github-actions[bot]",
                   GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
        git(repo, "read-tree", "--empty", env=env)
        for path, content in sorted(files.items()):
            blob = git(repo, "hash-object", "-w", "--stdin", content=content).decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, path, env=env)
        tree = git(repo, "write-tree", env=env).decode().strip()
        commit = git(repo, "commit-tree", tree, content=(
            f"Latest Binance market snapshot: {manifest['generated_at']}\n").encode(), env=env).decode().strip()
        require(snapshot_at(repo, commit) == files, "Committed snapshot differs from validated files")
        # Empty expected value means create only if absent. Existing data updates use
        # an explicit lease; only this dedicated ref is ever replaced. No parent means
        # exactly one reachable commit, rather than full histories accumulating daily.
        git(repo, "push", f"--force-with-lease={DATA_REF}:{expected}",
            "origin", f"{commit}:{DATA_REF}")
        remote_tip = git(repo, "ls-remote", "--heads", "origin", DATA_REF).decode().split()
        require(remote_tip and remote_tip[0] == commit, "data changed after publication")
        print(f"Published validated snapshot on data: {commit}")
        return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("latest", nargs="?", type=Path, default=Path("public/latest"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    publish_data(args.latest, args.repo)


if __name__ == "__main__":
    main()
