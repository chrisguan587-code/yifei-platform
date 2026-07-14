from __future__ import annotations

import ast
from pathlib import Path
import unittest

from yifei_platform import __version__


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
FORBIDDEN_ROOTS = {"yifei_v3", "yifei_v4"}


class RepositoryBoundaryTest(unittest.TestCase):
    def test_platform_does_not_import_application_packages(self) -> None:
        violations: list[str] = []
        for path in SOURCE_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    if module.split(".", 1)[0] in FORBIDDEN_ROOTS:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {module}")
        self.assertEqual([], violations)

    def test_package_version_is_exposed(self) -> None:
        self.assertEqual("0.5.0", __version__)


if __name__ == "__main__":
    unittest.main()
