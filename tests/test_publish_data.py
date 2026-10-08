from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import exporter as e
import publish_data as p
from test_exporter import CUTOFF, fixture


class DataPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = fixture()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.repo = root / "checkout"
        self.remote.mkdir()
        self.repo.mkdir()
        p.git(self.remote, "init", "--bare")
        p.git(self.repo, "init", "-b", "main")
        p.git(self.repo, "config", "user.name", "Publication test")
        p.git(self.repo, "config", "user.email", "publication@example.test")
        p.git(self.repo, "config", "core.autocrlf", "true")
        (self.repo / "README.md").write_text("Source code stays on main\n", encoding="utf-8")
        p.git(self.repo, "add", "README.md")
        p.git(self.repo, "commit", "-m", "Source")
        p.git(self.repo, "remote", "add", "origin", str(self.remote))
        p.git(self.repo, "push", "origin", "main")
        self.main = p.git(self.repo, "rev-parse", "HEAD")
        # A publisher must not discard existing staged work in its checkout.
        (self.repo / "staged.txt").write_text("Keep this staged file\n", encoding="utf-8")
        p.git(self.repo, "add", "staged.txt")
        self.index = p.git(self.repo, "ls-files", "--stage")
        self.latest = root / "export" / "latest"
        e.write_export(self.latest, self.datasets, CUTOFF)

    def tip(self):
        return p.git(self.remote, "rev-parse", p.DATA_REF).decode().strip()

    def test_first_publish_exact_bytes_single_root_main_and_index_untouched(self):
        commit = p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), commit)
        self.assertEqual(p.git(self.remote, "rev-list", "--count", "data").strip(), b"1")
        self.assertEqual(p.git(self.remote, "show", "-s", "--format=%P", "data").strip(), b"")
        self.assertEqual(p.snapshot_at(self.repo, commit), {
            f"latest/{path.name}": path.read_bytes() for path in self.latest.iterdir()})
        self.assertEqual(p.git(self.repo, "rev-parse", "HEAD"), self.main)
        self.assertEqual(p.git(self.remote, "rev-parse", "main"), self.main)
        self.assertEqual(p.git(self.repo, "ls-files", "--stage"), self.index)

    def test_replacement_removes_old_parts_and_does_not_accumulate_history(self):
        old = Path(self.temporary.name) / "old"
        e.write_export(old, self.datasets, CUTOFF - 1, max_bytes=100)
        p.publish_data(old, self.repo)
        commit = p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), commit)
        self.assertEqual(p.git(self.remote, "rev-list", "--count", "data").strip(), b"1")
        self.assertEqual(set(p.snapshot_at(self.repo, commit)), {
            "latest/manifest.json", "latest/1d-part-001.txt", "latest/4h-part-001.txt"})

    def test_identical_retry_is_no_op(self):
        first = p.publish_data(self.latest, self.repo)
        self.assertEqual(p.publish_data(self.latest, self.repo), first)
        self.assertEqual(self.tip(), first)

    def test_corrupt_export_preserves_remote_snapshot(self):
        first = p.publish_data(self.latest, self.repo)
        (self.latest / "1d-part-001.txt").write_bytes(b"corrupt\n")
        with self.assertRaises(ValueError):
            p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), first)

    def test_older_export_preserves_remote_snapshot(self):
        newer = Path(self.temporary.name) / "newer"
        e.write_export(newer, self.datasets, CUTOFF + 1)
        first = p.publish_data(newer, self.repo)
        with self.assertRaisesRegex(ValueError, "not newer"):
            p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), first)

    def test_failed_push_preserves_remote_snapshot(self):
        older = Path(self.temporary.name) / "older"
        e.write_export(older, self.datasets, CUTOFF - 1)
        first = p.publish_data(older, self.repo)
        git = p.git

        def fail_push(repo, *args, **kwargs):
            if args[0] == "push":
                raise RuntimeError("simulated push failure")
            return git(repo, *args, **kwargs)

        with patch.object(p, "git", fail_push), self.assertRaisesRegex(RuntimeError, "push failure"):
            p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), first)

    def test_concurrent_update_rejected_by_actual_git_lease(self):
        older = Path(self.temporary.name) / "older"
        e.write_export(older, self.datasets, CUTOFF - 1)
        first = p.publish_data(older, self.repo)
        tree = p.git(self.repo, "rev-parse", f"{first}^{{tree}}").decode().strip()
        rival = p.git(self.repo, "commit-tree", tree, content=b"Concurrent snapshot\n").decode().strip()
        git = p.git

        def race(repo, *args, **kwargs):
            if args[0] == "push":
                git(repo, "push", f"--force-with-lease={p.DATA_REF}:{first}",
                    "origin", f"{rival}:{p.DATA_REF}")
            return git(repo, *args, **kwargs)

        with patch.object(p, "git", race), self.assertRaisesRegex(RuntimeError, "push failed"):
            p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), rival)

    def test_unrelated_data_branch_is_not_overwritten(self):
        p.git(self.repo, "push", "origin", "HEAD:refs/heads/data")
        first = self.tip()
        with self.assertRaisesRegex(ValueError, "outside the managed"):
            p.publish_data(self.latest, self.repo)
        self.assertEqual(self.tip(), first)


if __name__ == "__main__":
    unittest.main()
