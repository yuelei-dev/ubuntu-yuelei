import importlib.util
import json
import os
import stat
import tempfile
import unittest
import types
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_release_external_boundaries_v1",
    ROOT / "tools/test_release_external_boundaries_v1.py",
)
boundaries = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundaries)
CONTRACT = json.loads((
    ROOT / "tools/test_release_external_boundaries_v1.json"
).read_text("utf-8"))


@unittest.skipUnless(os.name == "posix", "requires real POSIX lstat/symlink semantics")
class ExternalBoundaryPosixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.sha = "9f298737e19e4eea5df7d20bbfeffb466b0b4b2d"
        modes = {
            "home": 0o755, "home/ubuntu": 0o751,
            "home/ubuntu/hermes-ip12-releases": 0o755,
            "home/ubuntu/hermes-ip12-releases/" + self.sha: 0o775,
            "home/ubuntu/leadgen-A": 0o775, "home/ubuntu/leadgen-B": 0o775,
            "home/ubuntu/leadgen-server": 0o755,
            "home/ubuntu/leadgen-server/files": 0o775,
        }
        for relative, mode in modes.items():
            path = self.root / relative
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
        for name in ("discovered.json", "jobs.db", "keywords.json"):
            path = self.root / "home/ubuntu/leadgen-server" / name
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
        os.symlink("/home/ubuntu/hermes-ip12-releases/" + self.sha,
                   self.root / "home/ubuntu/hermes-web")
        for side in ("A", "B"):
            for name in ("discovered.json", "files", "jobs.db", "keywords.json"):
                os.symlink("/home/ubuntu/leadgen-server/" + name,
                           self.root / ("home/ubuntu/leadgen-%s" % side) / name)
        self.policy = boundaries.ExternalBoundaryPolicy(
            CONTRACT, runtime_root=self.root,
            owner_resolver=lambda _name: self.uid,
            group_resolver=lambda _name: self.gid,
        )

    def test_declared_external_code_and_shared_data_are_never_write_snapshots(self):
        snapshot = self.policy.snapshot()
        self.assertEqual(9, len(snapshot))
        self.assertTrue(all(item["write_policy"] == "never_follow_never_write"
                            for item in snapshot))
        self.assertEqual(snapshot, self.policy.verify_snapshot(snapshot))
        shared = self.root / "home/ubuntu/leadgen-server/jobs.db"
        shared.write_text('{"mutable":true}', encoding="utf-8")
        shared.chmod(0o644)
        self.assertEqual(snapshot, self.policy.verify_snapshot(snapshot))

    def test_home_ubuntu_requires_exact_observed_0751(self):
        home_ubuntu = self.root / "home/ubuntu"
        self.assertEqual(0o751, stat.S_IMODE(os.lstat(home_ubuntu).st_mode))
        self.policy.snapshot()
        for mode in (0o755, 0o775, 0o777):
            with self.subTest(mode=oct(mode)):
                home_ubuntu.chmod(mode)
                with self.assertRaisesRegex(
                        boundaries.BoundaryError, r"external target parent is unsafe: /home/ubuntu/hermes-web"):
                    self.policy.snapshot()
                home_ubuntu.chmod(0o751)

    def test_relative_escape_dangling_chain_target_parent_and_mode_fail_closed(self):
        hermes = self.root / "home/ubuntu/hermes-web"
        cases = []
        hermes.unlink(); os.symlink("../../etc", hermes)
        cases.append(("target", lambda: None))
        for message, _ in cases:
            with self.assertRaisesRegex(boundaries.BoundaryError, message):
                self.policy.snapshot()
        hermes.unlink(); os.symlink("/home/ubuntu/hermes-ip12-releases/" + self.sha, hermes)
        target = self.root / "home/ubuntu/hermes-ip12-releases" / self.sha
        target.rmdir()
        with self.assertRaisesRegex(boundaries.BoundaryError, "dangling"):
            self.policy.snapshot()
        os.symlink("/home/ubuntu/leadgen-server", target)
        with self.assertRaisesRegex(boundaries.BoundaryError, "target metadata"):
            self.policy.snapshot()
        target.unlink(); target.mkdir(); target.chmod(0o755)
        with self.assertRaisesRegex(boundaries.BoundaryError, "target metadata"):
            self.policy.snapshot()
        target.chmod(0o775)
        parent = self.root / "home/ubuntu/hermes-ip12-releases"
        moved = self.root / "home/ubuntu/releases-real"
        parent.rename(moved); os.symlink("/home/ubuntu/releases-real", parent)
        with self.assertRaisesRegex(boundaries.BoundaryError, "target parent"):
            self.policy.snapshot()

    def test_link_target_and_post_check_replacement_fail_closed_with_runtime_path(self):
        hermes_path = "/home/ubuntu/hermes-web"
        snapshot = self.policy.snapshot()
        hermes = self.root / "home/ubuntu/hermes-web"
        hermes.unlink(); os.symlink("/home/ubuntu/hermes-ip12-releases/" + "a" * 40, hermes)
        with self.assertRaisesRegex(boundaries.BoundaryError, "hermes-web"):
            self.policy.verify_snapshot(snapshot)
        hermes.unlink(); os.symlink("/home/ubuntu/hermes-ip12-releases/" + self.sha, hermes)
        original = boundaries.os.readlink
        calls = 0

        def replaced(path):
            nonlocal calls
            value = original(path)
            if str(path) == str(hermes):
                calls += 1
                if calls == 2:
                    return "/home/ubuntu/hermes-ip12-releases/" + "b" * 40
            return value

        with mock.patch.object(boundaries.os, "readlink", side_effect=replaced), \
                self.assertRaisesRegex(boundaries.BoundaryError, hermes_path):
            self.policy.snapshot()

    def test_undeclared_repository_and_external_manager_governance_are_distinct(self):
        self.assertTrue(self.policy.governs_repository("server/hermes_ip12/app.py"))
        self.assertEqual("hermes-ip12-atomic-release",
                         self.policy.manager_for_repository("server/hermes_ip12/app.py"))
        self.assertTrue(self.policy.governs_repository("server/keywords.json"))
        self.assertFalse(self.policy.governs_repository("server/app.py"))
        with self.assertRaisesRegex(boundaries.BoundaryError, "ambiguous"):
            self.policy.manager_for_repository("server/app.py")


class ExternalBoundaryContractTests(unittest.TestCase):
    def test_contract_is_strict_and_contains_only_two_never_write_classes(self):
        policy = boundaries.ExternalBoundaryPolicy(
            CONTRACT, runtime_root="/", owner_resolver=lambda _name: 0,
            group_resolver=lambda _name: 0,
        )
        self.assertEqual({"external_code_root", "shared_runtime_data"},
                         {item["kind"] for item in policy.contracts})
        self.assertTrue(all(item["write_policy"] == "never_follow_never_write"
                            for item in policy.contracts))
        home_modes = [parent["modes"] for item in policy.contracts
                      for parent in item["link_parent_contracts"] + item["parent_contracts"]
                      if parent["path"] == "/home/ubuntu"]
        self.assertTrue(home_modes)
        self.assertTrue(all(modes == frozenset({0o751}) for modes in home_modes))
        expected_parent_modes = {
            "/home": frozenset({0o755}),
            "/home/ubuntu/hermes-ip12-releases": frozenset({0o755}),
            "/home/ubuntu/leadgen-A": frozenset({0o775}),
            "/home/ubuntu/leadgen-B": frozenset({0o775}),
            "/home/ubuntu/leadgen-server": frozenset({0o755}),
        }
        actual = {parent["path"]: parent["modes"] for item in policy.contracts
                  for parent in item["link_parent_contracts"] + item["parent_contracts"]
                  if parent["path"] in expected_parent_modes}
        self.assertEqual(expected_parent_modes, actual)
        altered = json.loads(json.dumps(CONTRACT))
        altered["external_code_roots"][0]["write_policy"] = "follow"
        with self.assertRaisesRegex(boundaries.BoundaryError, "metadata"):
            boundaries.ExternalBoundaryPolicy(altered, owner_resolver=lambda _: 0,
                                              group_resolver=lambda _: 0)

    def test_effective_catalog_removes_only_declared_never_write_mappings(self):
        class RuntimeCatalog:
            @staticmethod
            def mappings(subject, repository_path, strict_candidate=True):
                return [repository_path]

        class ReleaseEngine:
            pass

        class ReleaseError(RuntimeError):
            pass

        phase = types.SimpleNamespace(
            RuntimeCatalog=RuntimeCatalog, ReleaseEngine=ReleaseEngine,
            ReleaseError=ReleaseError,
            collect_release_impact=lambda *_args, **_kwargs: {"ok": True},
        )
        catalog = types.SimpleNamespace(
            rules=[
                {"repository": "server/hermes_ip12/"},
                {"repository": "server/keywords.json"},
                {"repository": "server/app.py"},
            ],
            inventory_roots=("/home/ubuntu/hermes-web/", "/home/ubuntu/leadgen-A/"),
            runtime_data_contracts=[
                {"path": "/home/ubuntu/hermes-web/data/", "kind": "mutable_directory"},
                {"path": "/home/ubuntu/leadgen-A/jobs.db", "kind": "sqlite_file"},
                {"path": "/home/ubuntu/leadgen-A/jobs.db-wal", "kind": "sqlite_file"},
            ],
        )
        effective, _engine, policy = boundaries.install(
            phase, catalog, CONTRACT, runtime_root="/",
            owner_resolver=lambda _name: 0, group_resolver=lambda _name: 0,
        )
        self.assertEqual([{"repository": "server/app.py"}], effective.rules)
        self.assertEqual(("/home/ubuntu/leadgen-A/",), effective.inventory_roots)
        self.assertEqual([], effective.mappings("server/keywords.json"))
        self.assertEqual(["server/app.py"], effective.mappings("server/app.py"))
        self.assertIn("/home/ubuntu/leadgen-A/jobs.db-wal", effective.runtime_data_exact)
        self.assertNotIn("/home/ubuntu/leadgen-A/jobs.db", effective.runtime_data_exact)

        class Repo:
            @staticmethod
            def changed_paths(_older, _newer):
                return [("M", "server/hermes_ip12/app.py")]

        with self.assertRaisesRegex(ReleaseError, "hermes-ip12-atomic-release"):
            phase.collect_release_impact(Repo(), effective, "a", "b")
        self.assertTrue(policy.governs_repository("server/hermes_ip12/app.py"))


if __name__ == "__main__":
    unittest.main()
