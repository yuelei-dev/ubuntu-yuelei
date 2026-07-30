import hashlib
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SHELL_PATH = ROOT / "site" / "workbench" / "cloud-shell.js"
SCRIPT_PATH = ROOT / "site" / "workbench" / "script.html"


class CloudShellRuntimeCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = SHELL_PATH.read_text(encoding="utf-8")
        cls.script = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_script_cache_stamp_tracks_forward_ported_shell_bytes(self):
        content = SHELL_PATH.read_bytes().replace(b"\r\n", b"\n")
        stamp = hashlib.md5(content).hexdigest()[:8]
        self.assertIn(f"cloud-shell.js?v={stamp}", self.script)

    def test_invite_and_notification_runtime_abi_is_present(self):
        self.assertIn("{k:'invite',l:'邀请中心',i:'users'}", self.shell)
        self.assertIn("function ip12ProgressNotices(payload)", self.shell)
        self.assertIn("'/api/auth/notifications?limit=50'", self.shell)
        self.assertIn("'/api/gen/digital-ip/projects'", self.shell)
        self.assertIn("d.system_notices=", self.shell)
        self.assertIn("d.ip12_skips=", self.shell)

    def test_login_uses_one_form_submit_path_and_browser_metadata(self):
        self.assertIn("'<form id=\"hqLoginForm\">'", self.shell)
        self.assertIn('type="submit" class="hqlb" id="hqSub"', self.shell)
        self.assertIn(
            "document.getElementById('hqLoginForm').onsubmit=function(e){ "
            "e.preventDefault();",
            self.shell,
        )
        self.assertNotIn("document.getElementById('hqSub').onclick=", self.shell)
        self.assertIn('autocomplete="username" maxlength="64"', self.shell)
        self.assertIn('autocomplete="one-time-code" maxlength="6"', self.shell)
        self.assertIn('autocomplete="current-password" maxlength="128"', self.shell)
        self.assertIn('autocomplete="new-password" maxlength="128"', self.shell)

    def test_hidden_credentials_are_disabled_by_mode(self):
        self.assertIn("if(c) c.disabled=true;", self.shell)
        self.assertIn("if(p) p.disabled=_hqPhone;", self.shell)
        self.assertIn("if(p2) p2.disabled=true;", self.shell)
        self.assertIn("if(d) d.disabled=true;", self.shell)

    def test_membership_label_is_not_hard_coded_to_member(self):
        self.assertIn("function membershipRoleName(user)", self.shell)
        self.assertIn("if(!user||!user.membership_active) return '非会员';", self.shell)
        self.assertIn("experience:'体验官'", self.shell)
        self.assertIn("partner:'合伙人'", self.shell)
        self.assertIn("initiator:'发起人'", self.shell)
        self.assertIn("var role=membershipRoleName(u);", self.shell)

    def test_pr156_prompt_visibility_and_runtime_asset_stamp_coexist(self):
        self.assertIn('cloud-shell.js?v=642453c4', self.script)
        self.assertIn(
            'id="bdReverseResult" class="sc-card" style="display:block;"',
            self.script,
        )


if __name__ == "__main__":
    unittest.main()
