from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "check_secrets.py"
SPEC = spec_from_file_location("check_secrets", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_secrets = module_from_spec(SPEC)
SPEC.loader.exec_module(check_secrets)


class SecretScannerTests(unittest.TestCase):
    def test_blocks_labeled_binance_api_key(self):
        value = "A" * 64
        findings = check_secrets.findings_for_text("settings.py", f'BINANCE_API_KEY="{value}"\n')
        self.assertTrue(findings)

    def test_blocks_github_token(self):
        value = "gh" + "p_" + "A" * 36
        findings = check_secrets.findings_for_text("settings.py", f'TOKEN="{value}"\n')
        self.assertTrue(findings)

    def test_blocks_private_key_marker(self):
        marker = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
        findings = check_secrets.findings_for_text("private.pem", marker)
        self.assertTrue(findings)

    def test_blocks_labeled_personal_data(self):
        phone = "+886" + "912345678"
        national_id = "A" + "123456789"
        findings = check_secrets.findings_for_text(
            "profile.json",
            f'personal_phone="{phone}"\nnational_id="{national_id}"\n',
        )
        self.assertEqual(2, len(findings))

    def test_allows_python_formatted_values(self):
        text = 'TOKEN="{value}"\npersonal_phone="{phone}"\n'
        self.assertEqual([], check_secrets.findings_for_text("test_module.py", text))

    def test_allows_scanner_module_assignment(self):
        text = "check_secrets = module_from_spec(SPEC)\n"
        self.assertEqual([], check_secrets.findings_for_text("test_module.py", text))

    def test_blocks_wrapped_literal_that_is_not_a_named_placeholder(self):
        value = "C" * 64
        api_label = "BINANCE_API_" + "KEY"
        token_label = "TO" + "KEN"
        text = f'{api_label}="<{value}>"\n{token_label}="{{{value}}}"\n'
        self.assertEqual(2, len(check_secrets.findings_for_text("settings.py", text)))

    def test_blocks_sensitive_binary_path_even_without_text(self):
        self.assertTrue(check_secrets.findings_for_path("credentials/account.p12"))

    def test_blocks_any_binary_blob_fail_closed(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = check_secrets._scan([("records.zip", b"PK\x00private")])
        self.assertEqual(1, result)

    def test_blocks_nul_free_invalid_utf8_binary(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = check_secrets._scan([("artifact.bin", bytes((255, 254, 253, 252)))])
        self.assertEqual(1, result)

    def test_blocks_ascii_pdf_magic_without_nul(self):
        blob = b"%PDF-1.7\n1 0 obj\nendobj\n%%EOF\n"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = check_secrets._scan([("artifact", blob)])
        self.assertEqual(1, result)

    def test_diagnostics_redact_sensitive_path(self):
        value = "F" * 64
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = check_secrets._scan([("secrets/" + value + ".txt", b"safe\n")])
        self.assertEqual(1, result)
        self.assertNotIn(value, output.getvalue())

    def test_blocks_additional_sensitive_paths(self):
        paths = ("vault.kdbx", "client.ovpn", "private.p8", ".netrc")
        self.assertTrue(all(check_secrets.findings_for_path(path) for path in paths))

    def test_blocks_unlabeled_high_entropy_token(self):
        value = "".join(("Ab3dEf5h", "Ij7kLm9n", "Op2qRs4t", "Uv6wXy8z"))
        self.assertTrue(check_secrets.findings_for_text("config.txt", value))

    def test_blocks_common_unlabeled_personal_data(self):
        email = "hanqin" + "@" + "private-mail.tw"
        phone = "+886" + "912345678"
        national_id = "A" + "123456789"
        text = "\n".join((email, phone, national_id))
        self.assertEqual(3, len(check_secrets.findings_for_text("contacts.txt", text)))

    def test_does_not_accept_placeholder_like_secret_literals(self):
        token_label = "TO" + "KEN"
        key_label = "API_" + "KEY"
        text = (
            f'{token_label}="prefix_os.environ_actual_secret"\n'
            f'{key_label}="dummy_actual_secret_value"\n'
        )
        self.assertEqual(2, len(check_secrets.findings_for_text("settings.py", text)))

    def test_allows_non_secret_label_suffixes(self):
        text = (
            'token_count="123456"\n'
            'password_hash="abcdef0123456789"\n'
            'api_key_name="production"\n'
        )
        self.assertEqual([], check_secrets.findings_for_text("metrics.py", text))

    def test_allows_env_example_path_for_placeholder_only_content(self):
        self.assertEqual([], check_secrets.findings_for_path("service.env.example"))

    def test_allows_placeholders_and_environment_reads(self):
        api_label = "BINANCE_API_" + "KEY"
        token_label = "TO" + "KEN"
        text = (
            f'{api_label}="${{BINANCE_API_KEY}}"\n'
            f'{token_label}=os.environ["TOKEN"]\n'
        )
        self.assertEqual([], check_secrets.findings_for_text("settings.py", text))

    def test_allows_unlabeled_sha256(self):
        value = "a" * 64
        self.assertEqual([], check_secrets.findings_for_text("checksums.txt", value))

    def test_allows_public_url_slug(self):
        url = "https://github.com/HCH725/nautilus-quant-system"
        self.assertEqual([], check_secrets.findings_for_text("pyproject.toml", url))

    def test_allows_non_secret_token_mapping(self):
        text = "_INTERVAL_TO" + "KEN = {\n"
        self.assertEqual([], check_secrets.findings_for_text("module.py", text))

    def test_staged_scan_uses_index_bytes_and_redacts_value(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="secret-index-proof-") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            label = "BINANCE_API_" + "KEY"
            value = "E" * 64
            target = repo / "settings.py"
            target.write_text(f'{label}="{value}"\n')
            subprocess.run(["git", "add", "settings.py"], cwd=repo, check=True, capture_output=True)
            target.write_text(f'{label}="${{{label}}}"\n')
            output = io.StringIO()
            try:
                os.chdir(repo)
                with redirect_stdout(output), redirect_stderr(output):
                    result = check_secrets._scan(check_secrets._staged_entries())
            finally:
                os.chdir(previous)
        self.assertEqual(1, result)
        self.assertNotIn(value, output.getvalue())

    def test_malformed_pre_push_input_fails_closed(self):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("malformed\n")):
            with redirect_stdout(output), redirect_stderr(output):
                result = check_secrets.main(["--pre-push"])
        self.assertEqual(2, result)
        self.assertIn("failed closed", output.getvalue())

    def test_pre_push_scans_merge_and_new_branch_but_allows_deletion(self):
        def git(repo, *args):
            subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )

        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="secret-merge-proof-") as directory:
            repo = Path(directory)
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Scanner Test")
            git(repo, "config", "user.email", "scanner@example.invalid")
            (repo / "root.txt").write_text("root\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "root")
            root = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            git(repo, "switch", "-c", "side")
            (repo / "side.txt").write_text("side\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "side")
            git(repo, "switch", "main")
            (repo / "main.txt").write_text("main\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "main")
            git(repo, "merge", "--no-ff", "--no-commit", "side")
            label = "BINANCE_API_" + "KEY"
            (repo / "merge_only.py").write_text(f'{label}="{"D" * 64}"\n')
            git(repo, "add", ".")
            git(repo, "commit", "-m", "merge")
            local = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            line = f"refs/heads/main {local} refs/heads/main {root}\n"
            new_branch = f"refs/heads/main {local} refs/heads/main {'0' * 40}\n"
            deletion = f"refs/heads/main {'0' * 40} refs/heads/main {local}\n"
            try:
                os.chdir(repo)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = check_secrets._scan(check_secrets._pre_push_entries([line]))
                    new_result = check_secrets._scan(
                        check_secrets._pre_push_entries([new_branch])
                    )
                    deletion_result = check_secrets._scan(
                        check_secrets._pre_push_entries([deletion])
                    )
            finally:
                os.chdir(previous)
        self.assertEqual(1, result)
        self.assertEqual(1, new_result)
        self.assertEqual(0, deletion_result)


if __name__ == "__main__":
    unittest.main()
