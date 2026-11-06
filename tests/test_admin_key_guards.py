"""Focused fail-closed guard tests without importing the production application.

The app module has database initialization side effects; load only the guard
functions from its AST so tests never touch production services or secrets.
"""
import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class GuardHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def load_guard(relative_path, function_name, admin_key):
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    module = ast.parse(source, filename=relative_path)
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == function_name]
    assert len(functions) == 1, f"Missing or duplicate guard: {function_name}"
    namespace = {"HTTPException": GuardHTTPException, "ADMIN_KEY": admin_key, "os": os}
    isolated = ast.Module(body=functions, type_ignores=[])
    exec(compile(ast.fix_missing_locations(isolated), relative_path, "exec"), namespace)
    return namespace[function_name]


class AdminKeyGuardTests(unittest.TestCase):
    def test_main_guard_missing_empty_incorrect_and_valid(self):
        for configured in (None, "", "   "):
            with self.subTest(configured=configured):
                guard = load_guard("app/main.py", "_require_admin", configured)
                with self.assertRaises(GuardHTTPException) as caught:
                    guard("provided-key")
                self.assertEqual(caught.exception.status_code, 401)
        guard = load_guard("app/main.py", "_require_admin", "correct-key")
        for supplied in (None, "", "wrong-key"):
            with self.subTest(supplied=supplied):
                with self.assertRaises(GuardHTTPException) as caught:
                    guard(supplied)
                self.assertEqual(caught.exception.status_code, 401)
        self.assertIsNone(guard("correct-key"))

    def test_shared_guard_missing_empty_incorrect_and_valid(self):
        for configured in (None, "", "   "):
            guard = load_guard("app/core/admin_auth.py", "require_admin", configured)
            with self.assertRaises(GuardHTTPException) as caught:
                guard("provided-key")
            self.assertEqual(caught.exception.status_code, 401)
        guard = load_guard("app/core/admin_auth.py", "require_admin", "correct-key")
        for supplied in (None, "", "wrong-key"):
            with self.assertRaises(GuardHTTPException) as caught:
                guard(supplied)
            self.assertEqual(caught.exception.status_code, 401)
        self.assertIsNone(guard("correct-key"))

    def test_internal_launch_guard_missing_empty_incorrect_and_valid(self):
        guard = load_guard("app/main.py", "_require_internal_launch_key", "")
        for configured in (None, "", "  "):
            with self.subTest(configured=configured):
                env = {} if configured is None else {"SOFTSWISS_LAUNCH_INTERNAL_KEY": configured}
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(GuardHTTPException) as caught:
                        guard("provided-key")
                    self.assertEqual(caught.exception.status_code, 403)
        guard = load_guard("app/main.py", "_require_internal_launch_key", "fallback-key")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(guard("fallback-key"))
            with self.assertRaises(GuardHTTPException):
                guard("wrong-key")
        with patch.dict(os.environ, {"SOFTSWISS_LAUNCH_INTERNAL_KEY": "launch-key"}, clear=True):
            for supplied in (None, "", "wrong-key"):
                with self.assertRaises(GuardHTTPException) as caught:
                    guard(supplied)
                self.assertEqual(caught.exception.status_code, 403)
            self.assertIsNone(guard("launch-key"))

    def test_no_credential_fragments_logged(self):
        source = (ROOT / "app/main.py").read_text(encoding="utf-8")
        guard = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_require_admin")
        guard_source = ast.get_source_segment(source, guard)
        self.assertNotIn("print(", guard_source)
        self.assertNotIn("expected_prefix", guard_source)
        self.assertNotIn("received_prefix", guard_source)
        self.assertNotIn("ADMIN_DEBUG", guard_source)


if __name__ == "__main__":
    unittest.main()
