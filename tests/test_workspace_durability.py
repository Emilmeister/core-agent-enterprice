"""Full-tree object persistence, fresh-cache restore and hostile tree boundaries."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from core_agent.errors import CoreError
from core_agent.postgres_workspace import PostgresWorkspaceDurability
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from core_agent.workspace_durability import WorkspaceDurability, prune_manifest


class Blobs:
    def __init__(self):
        self.objects, self.puts, self.gets = {}, [], []
        self.after_put = None

    def put(self, content):
        digest = hashlib.sha256(content).hexdigest()
        self.puts.append(digest)
        self.objects[digest] = content
        if self.after_put:
            self.after_put()
        return digest

    def get(self, digest, size):
        self.gets.append(digest)
        return self.objects[digest]


class WorkspaceDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspaces = ChatWorkspaces(self.root / "first")
        self.binding = WorkspaceBinding("company", "owner", "chat")
        self.blobs = Blobs()
        self.service = WorkspaceDurability(self.workspaces, self.blobs, max_bytes=1024, max_files=30)

    def fresh(self):
        workspaces = ChatWorkspaces(self.root / "fresh")
        return workspaces, WorkspaceDurability(workspaces, self.blobs, max_bytes=1024, max_files=30)

    def assert_code(self, code, action):
        with self.assertRaises(CoreError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_empty_and_unchanged_checkpoints_do_not_upload(self):
        empty = self.service.prepare(self.binding, None)
        self.assertEqual(empty["entries"], {})
        folder = self.workspaces.workspace(self.binding)
        (folder / "data").write_bytes(b"bytes")
        manifest = self.service.prepare(self.binding, empty)
        self.assertEqual(self.service.prepare(self.binding, manifest), manifest)
        self.assertEqual(len(self.blobs.puts), 1)
        (folder / "data").unlink()
        self.assertEqual(self.service.prepare(self.binding, manifest), empty)
        self.assertEqual(len(self.blobs.puts), 1)

    def test_full_project_roundtrip_keeps_hidden_files_directories_modes_times_and_links(self):
        folder = self.workspaces.workspace(self.binding)
        for name in (".git/objects", ".tmp", "empty", "readonly"):
            (folder / name).mkdir(parents=True)
        for name in (".git/config", ".tmp/keep", "readonly/script"):
            (folder / name).write_bytes(name.encode())
        (folder / "readonly/script").chmod(0o510)
        os.utime(folder / "readonly/script", ns=(1_650_000_000_123_456_789,) * 2)
        os.utime(folder / ".git/objects", ns=(1_600_000_000_123_456_789,) * 2)
        (folder / "readonly").chmod(0o500)
        (folder / "link").symlink_to("readonly/script")
        manifest = self.service.prepare(self.binding, None)
        workspaces, restored = self.fresh()
        restored.restore(self.binding, manifest)
        target = workspaces.workspace(self.binding)
        self.assertEqual((target / ".tmp/keep").read_bytes(), b".tmp/keep")
        self.assertTrue((target / "empty").is_dir())
        self.assertEqual((target / "readonly/script").stat().st_mode & 0o777, 0o510)
        self.assertEqual((target / "readonly").stat().st_mode & 0o777, 0o500)
        self.assertEqual(os.readlink(target / "link"), "readonly/script")
        self.assertEqual(restored.prepare(self.binding, manifest), manifest)
        self.assertEqual(len(self.blobs.puts), 3)

    def test_same_bytes_reused_across_files_and_manifest_changes(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "one").write_bytes(b"same")
        (folder / "two").write_bytes(b"same")
        manifest = self.service.prepare(self.binding, None)
        self.assertEqual(len(self.blobs.puts), 1)
        (folder / "one").chmod(0o500)
        changed = self.service.prepare(self.binding, manifest)
        self.assertNotEqual(changed, manifest)
        self.assertEqual(len(self.blobs.puts), 1)

    def test_invalid_scope_paths_types_and_links_fail_before_reads(self):
        empty = self.service.prepare(self.binding, None)
        invalid = []
        wrong_scope = {**empty, "scope": "0" * 64}
        invalid.append((wrong_scope, "manifest_schema"))
        for path, entry, reason in (
                ("../escape", {"type": "directory", "mode": 0o700, "mtime_ns": 1}, "unsafe_path"),
                ("missing/child", {"type": "directory", "mode": 0o700, "mtime_ns": 1}, "manifest_parent"),
                ("link", {"type": "symlink", "mode": 0o777, "mtime_ns": 1, "target": "../outside"}, "unsafe_symlink"),
                ("node", {"type": "device", "mode": 0o600, "mtime_ns": 1}, "manifest_metadata"),
                ("time", {"type": "directory", "mode": 0o700, "mtime_ns": 1.5}, "manifest_metadata")):
            invalid.append(({**empty, "entries": {path: entry}}, reason))
        for manifest, reason in invalid:
            with self.subTest(manifest=manifest):
                _, restored = self.fresh()
                error = self.assert_code("WORKSPACE_SNAPSHOT_INVALID", lambda: restored.restore(self.binding, manifest))
                self.assertEqual(error.data, {"reason": reason})
                self.assertEqual(str(error), "WORKSPACE_SNAPSHOT_INVALID")
        self.assertEqual(self.blobs.gets, [])

    def test_unsafe_nodes_never_follow_outside_workspace(self):
        folder = self.workspaces.workspace(self.binding)
        outside = self.root / "outside"
        outside.write_bytes(b"secret")
        events = []
        coordinator = PostgresWorkspaceDurability(None, self.service, None)
        coordinator.log = lambda event, **fields: events.append((event, fields))
        for kind, reason in (("symlink", "unsafe_symlink"), ("hardlink", "hardlink"),
                             ("fifo", "unsupported_node"), ("setuid", "unsafe_mode")):
            with self.subTest(kind=kind):
                target = folder / "unsafe"
                if kind == "symlink":
                    target.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, target)
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.write_bytes(b"private contents")
                    target.chmod(0o4600)
                error = self.assert_code("WORKSPACE_SNAPSHOT_INVALID", lambda: self.service.prepare(self.binding, None))
                target.unlink()
                self.assertEqual(error.data, {"reason": reason})
                self.assertEqual(str(error), "WORKSPACE_SNAPSHOT_INVALID")
                coordinator._report("checkpoint", error=error)
                self.assertEqual(events[-1], ("workspace.storage", {
                    "operation": "checkpoint", "outcome": "failed", "file_count": 0, "bytes": 0,
                    "error_code": "WORKSPACE_SNAPSHOT_INVALID", "reason": reason}))
        for reason in (str(outside), ["unsafe_path"]):
            coordinator._report("checkpoint", error=CoreError("WORKSPACE_SNAPSHOT_INVALID", data={"reason": reason}))
            self.assertNotIn("reason", events[-1][1])
        self.assertEqual(self.blobs.puts, [])

    def test_tree_change_during_upload_cannot_commit_a_snapshot(self):
        folder = self.workspaces.workspace(self.binding)
        source = folder / "file"
        source.write_bytes(b"before")
        self.blobs.after_put = lambda: source.write_bytes(b"after")
        self.assert_code("WORKSPACE_SNAPSHOT_CHANGED", lambda: self.service.prepare(self.binding, None))

    def test_symlink_parent_traversal_after_another_link_cannot_escape(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "sub").mkdir()
        (folder / "sub/alias").symlink_to(".")
        (folder / "escape").symlink_to("sub/alias/../../outside")
        self.assert_code("WORKSPACE_SNAPSHOT_INVALID", lambda: self.service.prepare(self.binding, None))

    def test_bounds_fail_without_silently_dropping_files(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "large").write_bytes(b"x" * 1025)
        self.assert_code("WORKSPACE_SNAPSHOT_LIMIT", lambda: self.service.prepare(self.binding, None))
        (folder / "large").unlink()
        for index in range(31):
            (folder / str(index)).touch()
        self.assert_code("WORKSPACE_SNAPSHOT_LIMIT", lambda: self.service.prepare(self.binding, None))

    def test_corrupt_blob_never_publishes_partial_restore(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "one").write_bytes(b"first")
        (folder / "two").write_bytes(b"second")
        manifest = self.service.prepare(self.binding, None)
        self.blobs.objects[manifest["entries"]["two"]["sha256"]] = b"broken"
        workspaces, restored = self.fresh()
        self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: restored.restore(self.binding, manifest))
        self.assertEqual(workspaces.preview(self.binding)["files"], [])
        self.assertFalse(any(path.name.startswith(".restoring-") for path in workspaces.root.rglob("*")))

    def test_restore_does_not_overlay_live_tree(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "old").write_bytes(b"old")
        manifest = self.service.prepare(self.binding, None)
        (folder / "current").write_bytes(b"current")
        self.assert_code("WORKSPACE_RESTORE_CONFLICT", lambda: self.service.restore(self.binding, manifest))
        self.assertEqual((folder / "current").read_bytes(), b"current")
        self.assertEqual(self.blobs.gets, [])

    def test_missing_local_cache_cannot_erase_a_committed_manifest(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "data").write_bytes(b"value")
        manifest = self.service.prepare(self.binding, None)
        _, restored = self.fresh()
        self.assert_code("WORKSPACE_UNAVAILABLE", lambda: restored.prepare(self.binding, manifest))

    def test_pruning_removes_deleted_paths_without_uploads_or_old_bytes_returning(self):
        folder = self.workspaces.workspace(self.binding)
        (folder / "dir").mkdir()
        (folder / "dir/removed").write_bytes(b"gone")
        (folder / "retained").write_bytes(b"kept")
        manifest = self.service.prepare(self.binding, None)
        before = copy.deepcopy(manifest)
        pruned = prune_manifest(manifest, ["dir/removed"])
        self.assertEqual(manifest, before)
        self.assertIn("dir", pruned["entries"])
        workspaces, restored = self.fresh()
        restored.restore(self.binding, pruned)
        target = workspaces.workspace(self.binding)
        self.assertFalse((target / "dir/removed").exists())
        self.assertEqual((target / "retained").read_bytes(), b"kept")
        self.assertEqual(len(self.blobs.puts), 2)
