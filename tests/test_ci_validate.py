from pathlib import Path, PurePosixPath
from unittest import TestCase

from scripts.ci_validate import (
    candidate_paths,
    check_redlines,
    is_dynamic_or_external,
)


class RedlineTests(TestCase):
    def test_rejects_private_data_and_credentials(self) -> None:
        files = [
            PurePosixPath("data/leads.csv"),
            PurePosixPath("browser_data/cookies.json"),
            PurePosixPath("server/jobs.db"),
            PurePosixPath("config/.env.production"),
            PurePosixPath("deploy/private.key"),
        ]

        self.assertEqual(len(check_redlines(files)), len(files))

    def test_allows_normal_project_files(self) -> None:
        files = [
            PurePosixPath("site/index.html"),
            PurePosixPath("server/app.py"),
            PurePosixPath("docs/部署记录.md"),
        ]

        self.assertEqual(check_redlines(files), [])


class HtmlReferenceTests(TestCase):
    def test_extensionless_workbench_link_resolves_to_html(self) -> None:
        source = Path("site/workbench/audio.html")
        candidates = candidate_paths(source, "dashboard")

        self.assertIn(Path("site/workbench/dashboard.html"), candidates)

    def test_external_and_dynamic_references_are_ignored(self) -> None:
        self.assertTrue(is_dynamic_or_external("https://example.com/a.png"))
        self.assertTrue(is_dynamic_or_external("${result.url}"))
        self.assertTrue(is_dynamic_or_external("#pricing"))
        self.assertFalse(is_dynamic_or_external("../assets/cloud.css?v=8"))


class WorkflowTests(TestCase):
    def setUp(self) -> None:
        self.workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

    def test_media_dependencies_reuse_preinstalled_ffmpeg(self) -> None:
        self.assertIn("command -v ffmpeg", self.workflow)
        self.assertIn("command -v ffprobe", self.workflow)
        self.assertIn("install -y --no-install-recommends ffmpeg", self.workflow)

    def test_media_dependency_fallback_is_bounded(self) -> None:
        self.assertGreaterEqual(self.workflow.count("sudo timeout 4m apt-get"), 2)
        self.assertGreaterEqual(self.workflow.count("Acquire::Retries=3"), 2)
        self.assertGreaterEqual(self.workflow.count("Acquire::http::Timeout=20"), 2)
        self.assertGreaterEqual(self.workflow.count("Acquire::https::Timeout=20"), 2)

    def test_quality_job_has_time_for_full_test_suite(self) -> None:
        self.assertIn("timeout-minutes: 45", self.workflow)
