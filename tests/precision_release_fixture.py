import pathlib
import subprocess
import tempfile


def materialize_locked_source(test_case, repository_root, manifest):
    """Restore one historical release's exact candidate tree from Git blobs."""
    workspace = tempfile.TemporaryDirectory(prefix="precision-release-source-")
    test_case.addClassCleanup(workspace.cleanup)
    source_root = pathlib.Path(workspace.name)

    locked_sources = {
        item["repository_path"]: item["postimage_blob"]
        for item in manifest["files"]
    }
    executor = manifest["release_executor"]
    locked_sources[executor["repository_path"]] = executor["git_blob"]
    nginx = manifest["nginx_contract"]
    locked_sources[nginx["source_repository_path"]] = nginx["source_blob"]
    locked_sources[nginx["renderer_repository_path"]] = nginx["renderer_blob"]

    for repository_path, blob_id in locked_sources.items():
        data = subprocess.run(
            ["git", "cat-file", "blob", blob_id],
            cwd=repository_root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        target = source_root / repository_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return source_root
