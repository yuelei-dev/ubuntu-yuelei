import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "deploy" / "render_yuelei_test_nginx.py"
SOURCE = ROOT / "deploy" / "nginx-huangquechuanmei.conf"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_yuelei_test_nginx", RENDERER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class YueleiTestDomainNginxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = load_renderer()
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_rendered_vhost_directly_serves_test_runtime(self):
        rendered = self.renderer.render_config(self.source)
        self.assertIn("server_name yuelei.huangquechuanmei.com;", rendered)
        self.assertIn("root /var/www/huangquechuanmei;", rendered)
        self.assertIn("location ^~ /api/gen/", rendered)
        self.assertIn("location ^~ /api/auth/", rendered)
        self.assertIn("location ^~ /api/admin/", rendered)
        self.assertIn("proxy_set_header Host $host;", rendered)
        self.assertIn("/etc/letsencrypt/live/yuelei.huangquechuanmei.com/", rendered)

    def test_rendered_vhost_cannot_reenter_production_virtual_host(self):
        rendered = self.renderer.render_config(self.source)
        forbidden = (
            "proxy_pass https://127.0.0.1",
            "proxy_ssl_name huangquechuanmei.com",
            "proxy_set_header Host huangquechuanmei.com",
            "proxy_redirect https://huangquechuanmei.com",
            "server_name huangquechuanmei.com www.huangquechuanmei.com;",
            "/etc/letsencrypt/live/huangquechuanmei.com/",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, rendered)

    def test_rendered_listen_options_cannot_collide_with_main_vhost(self):
        # 测试服主站 block 已声明 [::]:443 的 ipv6only socket 选项；
        # 渲染产物重复声明会让 nginx -t 报 duplicate listen options。
        rendered = self.renderer.render_config(self.source)
        self.assertNotIn("ipv6only", rendered)
        self.assertIn("listen [::]:443 ssl;", rendered)
        self.assertIn("listen 443 ssl;", rendered)

    def test_only_digital_human_oneclick_allows_same_origin_framing(self):
        rendered = self.renderer.render_config(self.source)
        exception = self.renderer.ONECLICK_IFRAME_LOCATION

        self.assertIn(exception, rendered)
        self.assertIn("frame-ancestors 'none'", rendered)
        self.assertIn('X-Frame-Options "DENY"', rendered)
        self.assertIn("frame-ancestors 'self'", exception)
        self.assertIn('X-Frame-Options "SAMEORIGIN"', exception)
        self.assertIn(
            "try_files /workbench/digital-human-oneclick.html =404;",
            exception,
        )
        self.assertEqual(1, rendered.count("frame-ancestors 'self'"))
        self.assertEqual(1, rendered.count('X-Frame-Options "SAMEORIGIN"'))
        self.assertLess(
            rendered.index("location = /workbench/digital-human-oneclick"),
            rendered.rindex("    location / {"),
        )

    def test_login_home_admin_and_other_workbench_routes_remain_denied(self):
        rendered = self.renderer.render_config(self.source)
        exception_path = "location = /workbench/digital-human-oneclick"

        self.assertNotIn("location = /login", rendered)
        self.assertNotIn("location = /workbench/inspiration", rendered)
        self.assertNotIn("location = /admin", rendered)
        self.assertIn("location = / {", rendered)
        self.assertIn("location ^~ /api/admin/", rendered)
        self.assertEqual(1, rendered.count(exception_path))
        self.assertGreaterEqual(rendered.count("frame-ancestors 'none'"), 4)
        self.assertGreaterEqual(rendered.count('X-Frame-Options "DENY"'), 4)

    def test_renderer_fails_closed_if_global_frame_denial_drifts(self):
        for original, replacement in (
            ("frame-ancestors 'none'", "frame-ancestors https:"),
            ('X-Frame-Options "DENY"', 'X-Frame-Options "SAMEORIGIN"'),
        ):
            with self.subTest(original=original):
                damaged = self.source.replace(original, replacement)
                with self.assertRaisesRegex(self.renderer.RenderError, "missing"):
                    self.renderer.render_config(damaged)

    def test_same_origin_exception_passes_nginx_syntax_check_when_available(self):
        nginx = shutil.which("nginx")
        if nginx is None:
            self.skipTest("nginx executable is not available")

        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / "logs").mkdir()
            (prefix / "html" / "workbench").mkdir(parents=True)
            for name in (
                "client_body_temp",
                "proxy_temp",
                "fastcgi_temp",
                "uwsgi_temp",
                "scgi_temp",
            ):
                (prefix / "temp" / name).mkdir(parents=True)
            config = prefix / "nginx.conf"
            portable_prefix = str(prefix).replace(os.sep, "/")
            config.write_text(
                f"pid {portable_prefix}/nginx.pid;\n"
                f"error_log {portable_prefix}/logs/error.log;\n"
                "events {}\n"
                "http {\n"
                "    access_log off;\n"
                "    server {\n"
                "        listen 18080;\n"
                f"        root {portable_prefix}/html;\n"
                + self.renderer.ONECLICK_IFRAME_LOCATION
                + "    }\n"
                "}\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [nginx, "-t", "-p", str(prefix), "-c", str(config)],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(
                0,
                completed.returncode,
                completed.stdout + completed.stderr,
            )

    def test_http_redirect_and_clean_url_redirect_stay_on_test_origin(self):
        rendered = self.renderer.render_config(self.source)
        self.assertIn(
            "return 301 https://yuelei.huangquechuanmei.com$request_uri;",
            rendered,
        )
        self.assertIn("return 301 /$1$is_args$args;", rendered)
        self.assertNotIn("https://huangquechuanmei.com$request_uri", rendered)

    def test_renderer_fails_closed_when_reviewed_source_contract_drifts(self):
        damaged = self.source.replace(
            "location ^~ /api/auth/", "location ^~ /api/auth-broken/", 1
        )
        with self.assertRaisesRegex(self.renderer.RenderError, "missing"):
            self.renderer.render_config(damaged)

    def test_cli_writes_complete_config_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "yuelei-test.conf"
            subprocess.run(
                [
                    sys.executable,
                    str(RENDERER),
                    "--source",
                    str(SOURCE),
                    "--output",
                    str(output),
                ],
                check=True,
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertTrue(rendered.startswith("# Generated by"))
            if os.name == "posix":
                self.assertEqual(0o644, output.stat().st_mode & 0o777)
            self.assertNotIn(".yuelei-test.conf.", " ".join(p.name for p in output.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
