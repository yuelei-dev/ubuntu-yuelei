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
OWNER_IDS = {"root": 41001, "ubuntu": 41002}
GROUP_IDS = {"root": 42001, "ubuntu": 42002}


@unittest.skipUnless(os.name == "posix", "requires real POSIX lstat/symlink semantics")
class ExternalBoundaryPosixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner_ids = dict(OWNER_IDS)
        self.group_ids = dict(GROUP_IDS)
        self.hermes_releases_owner = "root"
        self.real_lstat = os.lstat
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
        self.lstat_patcher = mock.patch.object(
            boundaries.os, "lstat", side_effect=self._contract_lstat,
        )
        self.lstat_patcher.start()
        self.addCleanup(self.lstat_patcher.stop)
        self.policy = boundaries.ExternalBoundaryPolicy(
            CONTRACT, runtime_root=self.root,
            owner_resolver=self.owner_ids.__getitem__,
            group_resolver=self.group_ids.__getitem__,
        )

    def _contract_lstat(self, path):
        info = self.real_lstat(path)
        try:
            runtime_path = "/" + Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return info
        if runtime_path in {"/home", "/home/ubuntu/hermes-web"}:
            owner = "root"
        elif runtime_path == "/home/ubuntu/hermes-ip12-releases":
            owner = self.hermes_releases_owner
        else:
            owner = "ubuntu"
        return type("ContractStat", (), {
            "st_mode": info.st_mode,
            "st_uid": self.owner_ids[owner],
            "st_gid": self.group_ids[owner],
            "st_dev": info.st_dev,
            "st_ino": info.st_ino,
        })()

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
        self.assertEqual(0o751, stat.S_IMODE(self.real_lstat(home_ubuntu).st_mode))
        self.policy.snapshot()
        for mode in (0o755, 0o775, 0o777):
            with self.subTest(mode=oct(mode)):
                home_ubuntu.chmod(mode)
                with self.assertRaisesRegex(
                        boundaries.BoundaryError, r"external target parent is unsafe: /home/ubuntu/hermes-web"):
                    self.policy.snapshot()
                home_ubuntu.chmod(0o751)

    def test_hermes_releases_parent_requires_root_not_ubuntu_owner(self):
        self.assertNotEqual(self.owner_ids["root"], self.owner_ids["ubuntu"])
        self.assertNotEqual(self.group_ids["root"], self.group_ids["ubuntu"])
        self.policy.snapshot()
        self.hermes_releases_owner = "ubuntu"
        with self.assertRaisesRegex(
                boundaries.BoundaryError,
                r"external target parent is unsafe: /home/ubuntu/hermes-web"):
            self.policy.snapshot()

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
            CONTRACT, runtime_root="/", owner_resolver=OWNER_IDS.__getitem__,
            group_resolver=GROUP_IDS.__getitem__,
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
        hermes = next(item for item in policy.contracts
                      if item["kind"] == "external_code_root")
        releases_parent = next(parent for parent in hermes["parent_contracts"]
                               if parent["path"] == "/home/ubuntu/hermes-ip12-releases")
        self.assertEqual(("root", "root", frozenset({0o755})), (
            releases_parent["owner"], releases_parent["group"], releases_parent["modes"],
        ))
        altered = json.loads(json.dumps(CONTRACT))
        altered["external_code_roots"][0]["write_policy"] = "follow"
        with self.assertRaisesRegex(boundaries.BoundaryError, "metadata"):
            boundaries.ExternalBoundaryPolicy(
                altered, owner_resolver=OWNER_IDS.__getitem__,
                group_resolver=GROUP_IDS.__getitem__,
            )

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
            owner_resolver=OWNER_IDS.__getitem__, group_resolver=GROUP_IDS.__getitem__,
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


@unittest.skipUnless(os.name == "posix", "requires POSIX no-follow file semantics")
class MutableRuntimeMetadataPosixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_path = "/home/ubuntu/content-api/content_jobs.db"
        self.target = self.root / self.runtime_path.lstrip("/")
        self.target.parent.mkdir(parents=True)
        self.target.touch()
        self.target.chmod(0o640)
        self.uid = os.geteuid()
        self.gid = os.getegid()

        class RuntimeCatalog:
            @staticmethod
            def mappings(_subject, repository_path, strict_candidate=True):
                return [repository_path]

        class ReleaseError(RuntimeError):
            pass

        class ReleaseEngine:
            def __init__(subject, *, owner_resolver=None, group_resolver=None):
                subject.runtime_root = self.root
                subject.catalog = types.SimpleNamespace(runtime_data_contracts=[{
                    "path": self.runtime_path,
                    "kind": "sqlite_file",
                    "owner": "ubuntu",
                    "group": "ubuntu",
                    "allowed_modes": frozenset({0o640}),
                    "required": True,
                }])
                subject.owner_resolver = owner_resolver or (lambda _name: self.uid)
                subject.group_resolver = group_resolver or (lambda _name: self.gid)

            def _runtime_inventory(subject):
                return set(), set()

        phase = types.SimpleNamespace(
            RuntimeCatalog=RuntimeCatalog, ReleaseEngine=ReleaseEngine,
            ReleaseError=ReleaseError,
            _mapped_path=lambda root, path: Path(root) / path.lstrip("/"),
            _assert_real_parents=self._assert_real_parents,
            collect_release_impact=lambda *_args, **_kwargs: {"ok": True},
        )
        catalog = types.SimpleNamespace(
            rules=[], inventory_roots=(),
            runtime_data_contracts=ReleaseEngine().catalog.runtime_data_contracts,
        )
        _catalog, self.engine_class, _policy = boundaries.install(
            phase, catalog, {
                "schema_version": 1,
                "external_code_roots": [],
                "shared_runtime_data_links": [],
                "shared_link_defaults": CONTRACT["shared_link_defaults"],
            }, runtime_root=self.root,
            owner_resolver=lambda _name: self.uid,
            group_resolver=lambda _name: self.gid,
        )

    @staticmethod
    def _assert_real_parents(root, target, create=False):
        root = Path(os.path.abspath(root))
        current = root
        for part in Path(target).parent.relative_to(root).parts:
            current /= part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("runtime parent contains a symbolic link or non-directory")

    def engine(self, *, owner_resolver=None, group_resolver=None):
        return self.engine_class(
            owner_resolver=owner_resolver, group_resolver=group_resolver,
        )

    def test_large_sparse_sqlite_is_metadata_only_and_does_not_read_content(self):
        logical_size = 3 * 1024 * 1024 * 1024
        with self.target.open("r+b") as stream:
            stream.truncate(logical_size)
        self.assertEqual(logical_size, self.target.stat().st_size)
        with mock.patch.object(
                boundaries.os, "read",
                side_effect=AssertionError("mutable runtime data must not be read")) as reader:
            self.assertEqual(set(), self.engine()._runtime_data_drift())
        reader.assert_not_called()

    def test_symlink_and_parent_symlink_fail_closed(self):
        replacement = self.target.with_name("replacement.db")
        replacement.touch()
        replacement.chmod(0o640)
        self.target.unlink()
        self.target.symlink_to(replacement.name)
        with self.assertRaisesRegex(RuntimeError, self.runtime_path):
            self.engine()._runtime_data_drift()
        self.target.unlink()
        real_parent = self.target.parent.with_name("content-api-real")
        self.target.parent.rename(real_parent)
        self.target.parent.symlink_to(real_parent.name, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "runtime parent"):
            self.engine()._runtime_data_drift()

    def test_inode_replacement_during_open_fails_closed(self):
        real_fstat = boundaries.os.fstat
        replaced = False

        def replace_after_open(descriptor):
            nonlocal replaced
            info = real_fstat(descriptor)
            if not replaced:
                replaced = True
                self.target.rename(self.target.with_name("old.db"))
                self.target.touch()
                self.target.chmod(0o640)
            return info

        with mock.patch.object(boundaries.os, "fstat", side_effect=replace_after_open), \
                self.assertRaisesRegex(RuntimeError, "changed while it was inspected"):
            self.engine()._runtime_data_drift()

    def test_mode_owner_and_group_drift_are_reported(self):
        self.target.chmod(0o600)
        self.assertEqual({self.runtime_path}, self.engine()._runtime_data_drift())
        self.target.chmod(0o640)
        self.assertEqual({self.runtime_path}, self.engine(
            owner_resolver=lambda _name: self.uid + 1,
        )._runtime_data_drift())
        self.assertEqual({self.runtime_path}, self.engine(
            group_resolver=lambda _name: self.gid + 1,
        )._runtime_data_drift())


if __name__ == "__main__":
    unittest.main()
