"""Versioned, fail-closed external runtime-boundary contracts.

The immutable phase-one catalog predates two existing deployment layouts.  This
module does not make either layout writable.  It removes their explicitly
governed repository/runtime paths from the unified writer and replaces content
inventory with repeated lstat/readlink metadata contracts.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import types
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
CONTRACT_PATH = "/usr/local/share/huangque-release/test_release_external_boundaries_v1.json"
_MODE_RE = re.compile(r"^0[0-7]{3}$")
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class BoundaryError(RuntimeError):
    """An external boundary is invalid or changed."""


def _absolute(value):
    path = PurePosixPath(str(value or ""))
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
        raise BoundaryError("external boundary path is invalid: %s" % value)
    return path.as_posix()


def _mode(value):
    if not isinstance(value, str) or not _MODE_RE.fullmatch(value):
        raise BoundaryError("external boundary mode is invalid")
    return int(value, 8)


def _mapped(root, runtime_path):
    root = Path(os.path.abspath(root))
    path = PurePosixPath(_absolute(runtime_path))
    target = root.joinpath(*path.parts[1:])
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise BoundaryError("external boundary escaped runtime root: %s" % runtime_path)
    return target


def _identity(info, *, stable_object=True):
    value = {
        "mode": stat.S_IMODE(info.st_mode), "uid": int(info.st_uid),
        "gid": int(info.st_gid), "kind": stat.S_IFMT(info.st_mode),
    }
    if stable_object:
        value.update({"device": int(info.st_dev), "inode": int(info.st_ino)})
    return value


def _stable_metadata(info):
    return (
        int(info.st_dev), int(info.st_ino), stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode), int(info.st_uid), int(info.st_gid),
    )


def _real_parent_snapshot(phase_one, root, target):
    root = Path(os.path.abspath(root))
    phase_one._assert_real_parents(root, target)
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise phase_one.ReleaseError("runtime root must be a real directory")
    records = [(str(root), _stable_metadata(root_info))]
    current = root
    for part in Path(target).parent.relative_to(root).parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise phase_one.ReleaseError(
                "runtime parent contains a symbolic link or non-directory"
            )
        records.append((str(current), _stable_metadata(info)))
    return tuple(records)


def _read_mutable_regular_metadata(phase_one, root, runtime_path):
    """Open and validate a mutable file without reading any content bytes."""
    target = phase_one._mapped_path(root, runtime_path)
    parents_before = _real_parent_snapshot(phase_one, root, target)
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise phase_one.ReleaseError(
            "runtime target is not a regular file: %s" % runtime_path
        )
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise phase_one.ReleaseError(
            "runtime target changed while it was opened: %s" % runtime_path
        ) from exc
    try:
        opened = os.fstat(descriptor)
        try:
            after = os.lstat(target)
            parents_after = _real_parent_snapshot(phase_one, root, target)
        except OSError as exc:
            raise phase_one.ReleaseError(
                "runtime target changed while it was inspected: %s" % runtime_path
            ) from exc
        if (not stat.S_ISREG(opened.st_mode)
                or _stable_metadata(before) != _stable_metadata(opened)
                or _stable_metadata(after) != _stable_metadata(opened)
                or parents_before is None or parents_after != parents_before):
            raise phase_one.ReleaseError(
                "runtime target changed while it was inspected: %s" % runtime_path
            )
        return opened
    finally:
        os.close(descriptor)


class ExternalBoundaryPolicy:
    def __init__(self, data, *, runtime_root="/", owner_resolver=None, group_resolver=None):
        if (not isinstance(data, dict)
                or set(data) != {"schema_version", "external_code_roots",
                                 "shared_runtime_data_links", "shared_link_defaults"}
                or data.get("schema_version") != SCHEMA_VERSION):
            raise BoundaryError("external boundary contract fields are invalid")
        self.runtime_root = Path(runtime_root)
        self.owner_resolver = owner_resolver or (lambda name: __import__("pwd").getpwnam(name).pw_uid)
        self.group_resolver = group_resolver or (lambda name: __import__("grp").getgrnam(name).gr_gid)
        defaults = data["shared_link_defaults"]
        required_defaults = {"manager", "link_owner", "link_group", "link_mode",
                             "target_owner", "target_group", "link_parent_contracts",
                             "parent_contracts", "write_policy"}
        if not isinstance(defaults, dict) or set(defaults) != required_defaults:
            raise BoundaryError("shared runtime-data defaults are invalid")
        self.contracts = []
        for raw in data["external_code_roots"]:
            required = {"runtime_path", "manager", "repository_prefixes", "repository_paths",
                        "link_owner", "link_group", "link_mode", "target_pattern",
                        "target_kind", "target_owner", "target_group", "target_modes",
                        "link_parent_contracts", "parent_contracts", "write_policy"}
            if not isinstance(raw, dict) or set(raw) != required:
                raise BoundaryError("external code-root contract is invalid")
            item = dict(raw)
            item["kind"] = "external_code_root"
            item["target_regex"] = re.compile(item.pop("target_pattern"))
            self.contracts.append(self._normalize(item))
        for raw in data["shared_runtime_data_links"]:
            required = {"runtime_path", "target_path", "repository_paths",
                        "target_kind", "target_modes"}
            if not isinstance(raw, dict) or set(raw) != required:
                raise BoundaryError("shared runtime-data link contract is invalid")
            item = dict(defaults)
            item.update(raw)
            parent_key = str(PurePosixPath(_absolute(item["runtime_path"])).parent)
            item["link_parent_contracts"] = defaults["link_parent_contracts"].get(parent_key)
            if item["link_parent_contracts"] is None:
                raise BoundaryError("shared runtime-data link parent is undeclared")
            item.update({"kind": "shared_runtime_data", "repository_prefixes": []})
            item["target_regex"] = re.compile("^" + re.escape(_absolute(item.pop("target_path"))) + "$")
            self.contracts.append(self._normalize(item))
        paths = [item["runtime_path"] for item in self.contracts]
        if len(paths) != len(set(paths)):
            raise BoundaryError("external boundary runtime paths are duplicated")
        self.runtime_paths = frozenset(paths)
        self.code_roots = tuple(
            item["runtime_path"].rstrip("/") + "/" for item in self.contracts
            if item["kind"] == "external_code_root"
        )

    def _normalize(self, item):
        item = dict(item)
        item["runtime_path"] = _absolute(item["runtime_path"])
        if (item.get("target_kind") not in {"file", "directory"}
                or item.get("write_policy") != "never_follow_never_write"
                or not isinstance(item.get("manager"), str) or not item["manager"]
                or any(not _NAME_RE.fullmatch(str(item.get(key) or ""))
                       for key in ("link_owner", "link_group", "target_owner", "target_group"))):
            raise BoundaryError("external boundary metadata is invalid: %s" % item["runtime_path"])
        item["link_mode"] = _mode(item["link_mode"])
        item["target_modes"] = frozenset(_mode(value) for value in item["target_modes"])
        if not item["target_modes"]:
            raise BoundaryError("external boundary target modes are empty: %s" % item["runtime_path"])
        prefixes = item.get("repository_prefixes", [])
        paths = item.get("repository_paths", [])
        if (not isinstance(prefixes, list) or not isinstance(paths, list)
                or any(not isinstance(value, str) or not value.endswith("/") or value.startswith("/")
                       for value in prefixes)
                or any(not isinstance(value, str) or value.startswith("/") for value in paths)):
            raise BoundaryError("external repository governance is invalid")
        item["repository_prefixes"] = tuple(prefixes)
        item["repository_paths"] = frozenset(paths)
        def normalize_parents(raw_parents):
            parents = []
            for parent in raw_parents:
                if (not isinstance(parent, dict)
                        or set(parent) != {"path", "owner", "group", "modes"}
                        or not _NAME_RE.fullmatch(str(parent["owner"]))
                        or not _NAME_RE.fullmatch(str(parent["group"]))):
                    raise BoundaryError("external boundary parent contract is invalid")
                parents.append({"path": _absolute(parent["path"]), "owner": parent["owner"],
                                "group": parent["group"],
                                "modes": frozenset(_mode(value) for value in parent["modes"])})
            return tuple(parents)
        item["link_parent_contracts"] = normalize_parents(item["link_parent_contracts"])
        item["parent_contracts"] = normalize_parents(item["parent_contracts"])
        return item

    def governs_repository(self, repository_path):
        path = str(repository_path)
        return any(path in item["repository_paths"]
                   or any(path.startswith(prefix) for prefix in item["repository_prefixes"])
                   for item in self.contracts)

    def manager_for_repository(self, repository_path):
        path = str(repository_path)
        matches = [item["manager"] for item in self.contracts
                   if path in item["repository_paths"]
                   or any(path.startswith(prefix) for prefix in item["repository_prefixes"])]
        if len(set(matches)) != 1:
            raise BoundaryError("external repository contract is ambiguous: %s" % path)
        return matches[0]

    def _metadata_matches(self, info, owner, group, modes):
        return (info.st_uid == self.owner_resolver(owner)
                and info.st_gid == self.group_resolver(group)
                and stat.S_IMODE(info.st_mode) in modes)

    def _snapshot_one(self, contract):
        runtime_path = contract["runtime_path"]
        link = _mapped(self.runtime_root, runtime_path)
        try:
            link_info = os.lstat(link)
        except FileNotFoundError as exc:
            raise BoundaryError("external boundary link is missing: %s" % runtime_path) from exc
        if (not stat.S_ISLNK(link_info.st_mode)
                or not self._metadata_matches(link_info, contract["link_owner"],
                                              contract["link_group"], {contract["link_mode"]})):
            raise BoundaryError("external boundary link metadata is invalid: %s" % runtime_path)
        raw_target = os.readlink(link)
        if not raw_target.startswith("/") or not contract["target_regex"].fullmatch(raw_target):
            raise BoundaryError("external boundary link target is invalid: %s" % runtime_path)
        parent_records = []
        for parent in contract["link_parent_contracts"] + contract["parent_contracts"]:
            target_parent = _mapped(self.runtime_root, parent["path"])
            try:
                info = os.lstat(target_parent)
            except FileNotFoundError as exc:
                raise BoundaryError("external target parent is missing: %s" % runtime_path) from exc
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or not self._metadata_matches(info, parent["owner"], parent["group"], parent["modes"])):
                raise BoundaryError("external target parent is unsafe: %s" % runtime_path)
            parent_records.append({"path": parent["path"], "identity": _identity(info)})
        target = _mapped(self.runtime_root, raw_target)
        try:
            target_info = os.lstat(target)
        except FileNotFoundError as exc:
            raise BoundaryError("external boundary target is dangling: %s" % runtime_path) from exc
        expected_kind = stat.S_ISDIR if contract["target_kind"] == "directory" else stat.S_ISREG
        if (stat.S_ISLNK(target_info.st_mode) or not expected_kind(target_info.st_mode)
                or not self._metadata_matches(target_info, contract["target_owner"],
                                              contract["target_group"], contract["target_modes"])):
            raise BoundaryError("external boundary target metadata is invalid: %s" % runtime_path)
        try:
            stable = (os.readlink(link) == raw_target
                      and _identity(os.lstat(link)) == _identity(link_info)
                      and _identity(os.lstat(target)) == _identity(target_info)
                      and all(_identity(os.lstat(_mapped(self.runtime_root, record["path"])))
                              == record["identity"] for record in parent_records))
        except (FileNotFoundError, OSError):
            stable = False
        if not stable:
            raise BoundaryError("external boundary changed while inspected: %s" % runtime_path)
        target_identity = _identity(
            target_info, stable_object=contract["kind"] == "external_code_root",
        )
        return {"runtime_path": runtime_path, "kind": contract["kind"],
                "manager": contract["manager"], "write_policy": contract["write_policy"],
                "raw_target": raw_target, "link": _identity(link_info),
                "target": target_identity, "parents": parent_records}

    def snapshot(self):
        return [self._snapshot_one(item) for item in sorted(
            self.contracts, key=lambda value: value["runtime_path"])]

    def verify_snapshot(self, expected):
        current = self.snapshot()
        if current != expected:
            expected_paths = {item.get("runtime_path") for item in expected or [] if isinstance(item, dict)}
            current_paths = {item["runtime_path"] for item in current}
            path = sorted(expected_paths ^ current_paths)[0] if expected_paths ^ current_paths else "declared boundary"
            raise BoundaryError("external boundary snapshot changed: %s" % path)
        return current


def load_contract(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundaryError("external boundary JSON is invalid") from exc
    return value


def install(phase_one, catalog, contract, *, runtime_root="/", owner_resolver=None,
            group_resolver=None):
    """Return a boundary-aware catalog and ReleaseEngine class."""
    policy = ExternalBoundaryPolicy(contract, runtime_root=runtime_root,
                                    owner_resolver=owner_resolver,
                                    group_resolver=group_resolver)
    effective = copy.copy(catalog)
    effective.rules = [rule for rule in catalog.rules
                       if not policy.governs_repository(rule["repository"])]
    effective.inventory_roots = tuple(prefix for prefix in catalog.inventory_roots
                                      if prefix not in policy.code_roots)
    excluded_data = policy.runtime_paths | frozenset(
        contract["runtime_path"] for contract in policy.contracts
        if contract["kind"] == "external_code_root"
    )
    effective.runtime_data_contracts = [item for item in catalog.runtime_data_contracts
                                        if item["path"].rstrip("/") not in excluded_data
                                        and not any(item["path"].startswith(prefix)
                                                    for prefix in policy.code_roots)]
    effective.runtime_data_exact = {item["path"]: item for item in effective.runtime_data_contracts
                                    if item["kind"] != "mutable_directory"}
    effective.runtime_data_directories = [item for item in effective.runtime_data_contracts
                                          if item["kind"] == "mutable_directory"]
    base_mappings = phase_one.RuntimeCatalog.mappings

    def mappings(self, repository_path, *, strict_candidate=True):
        if policy.governs_repository(repository_path):
            return []
        return base_mappings(self, repository_path, strict_candidate=strict_candidate)

    effective.mappings = types.MethodType(mappings, effective)
    effective.external_boundary_policy = policy
    original_collect = phase_one.collect_release_impact

    def validate_coverage(repo, selected_catalog, commit):
        unmapped = []
        for path in repo.files_at(commit):
            if not selected_catalog.is_candidate(path) or selected_catalog.is_ignored(path):
                continue
            if policy.governs_repository(path):
                continue
            if not selected_catalog.mappings(path, strict_candidate=False):
                unmapped.append(path)
        if unmapped:
            raise phase_one.ReleaseError("runtime catalog leaves candidate files unclassified: %s"
                                         % ", ".join(sorted(unmapped)))

    def collect(repo, selected_catalog, older, newer, **kwargs):
        changed = [path for _status, path in repo.changed_paths(older, newer)
                   if policy.governs_repository(path)]
        if changed:
            details = ["%s (%s)" % (path, policy.manager_for_repository(path))
                       for path in sorted(changed)]
            raise phase_one.ReleaseError(
                "merged_not_releasable: external manager contract required: %s"
                % ", ".join(details))
        return original_collect(repo, selected_catalog, older, newer, **kwargs)

    phase_one.validate_catalog_coverage = validate_coverage
    phase_one.collect_release_impact = collect

    class BoundaryAwareEngine(phase_one.ReleaseEngine):
        def _runtime_data_drift(self):
            drift = set()
            for contract in self.catalog.runtime_data_contracts:
                runtime_path = contract["path"].rstrip("/")
                target = phase_one._mapped_path(self.runtime_root, runtime_path)
                expected_directory = contract["kind"] == "mutable_directory"
                if expected_directory:
                    phase_one._assert_real_parents(self.runtime_root, target)
                    try:
                        info = os.lstat(target)
                    except FileNotFoundError:
                        if contract["required"]:
                            drift.add(runtime_path)
                        continue
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        drift.add(runtime_path)
                        continue
                else:
                    info = _read_mutable_regular_metadata(
                        phase_one, self.runtime_root, runtime_path,
                    )
                    if info is None:
                        if contract["required"]:
                            drift.add(runtime_path)
                        continue
                if os.name == "posix" and (
                        stat.S_IMODE(info.st_mode) not in contract["allowed_modes"]
                        or info.st_uid != self.owner_resolver(contract["owner"])
                        or info.st_gid != self.group_resolver(contract["group"])):
                    drift.add(runtime_path)
            return drift

        def external_boundary_snapshot(self):
            try:
                return policy.snapshot()
            except BoundaryError as exc:
                raise phase_one.ReleaseError(str(exc)) from exc

        def verify_external_boundary_snapshot(self, expected):
            try:
                return policy.verify_snapshot(expected)
            except BoundaryError as exc:
                raise phase_one.ReleaseError(str(exc)) from exc

        def _runtime_inventory(self):
            self.external_boundary_snapshot()
            inventory, unsafe = super()._runtime_inventory()
            return inventory, set(unsafe) - set(policy.runtime_paths)

    return effective, BoundaryAwareEngine, policy
