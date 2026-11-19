"""T-B-032 / T-B-033 · 工程规范静态检查（import 图、路径、命名、契约哈希、依赖锁、片段同步）。

运行：python tests/test_engineering_rules.py -v
"""

import ast
import hashlib
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = ROOT / "model"
RESULTS = ROOT / "results"
BASELINE = ROOT / "tests" / "contract_baseline.json"

FORBIDDEN_PACKAGES = ("vision", "service", "dashboard", "scripts")
DL_FRAMEWORKS = ("torch", "tensorflow", "paddlepaddle", "mxnet")


def _imports(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _sha256(path: pathlib.Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


class TestImportGraph(unittest.TestCase):
    def test_no_cross_package_imports(self):
        for path in MODEL.glob("*.py"):
            imports = _imports(path)
            for package in FORBIDDEN_PACKAGES:
                offenders = [
                    name
                    for name in imports
                    if name == package or name.startswith(package + ".")
                ]
                self.assertEqual(offenders, [], f"{path.name} imports {package}")

    def test_imu_detect_is_isolated(self):
        imports = _imports(MODEL / "imu_detect.py")
        for forbidden in (
            "model.state_machine",
            "model.fuse",
            "model.reason",
            "model.eval",
        ):
            self.assertNotIn(forbidden, imports)

    def test_config_is_leaf(self):
        imports = _imports(MODEL / "config.py")
        internal = [name for name in imports if name.startswith("model.")]
        self.assertEqual(internal, [])
        self.assertNotIn("common.schema", imports)


class TestPathsAndNaming(unittest.TestCase):
    ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")

    def test_no_absolute_paths(self):
        for path in MODEL.glob("*.py"):
            self.assertIsNone(
                self.ABSOLUTE.search(path.read_text(encoding="utf-8")), path.name
            )
        for path in RESULTS.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".md", ".csv", ".jsonl"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertIsNone(self.ABSOLUTE.search(text), path.name)

    def test_result_file_naming(self):
        pattern = re.compile(r"^[a-z0-9_.]+$")
        data_suffixes = (".json", ".csv", ".png", ".jsonl")
        for path in RESULTS.rglob("*"):
            if not path.is_file():
                continue
            self.assertFalse(re.search(r"[\u4e00-\u9fff]", path.name), str(path))
            if path.suffix in data_suffixes:
                self.assertRegex(path.name, pattern, str(path))


class TestContractLock(unittest.TestCase):
    def test_contract_files_unchanged(self):
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(baseline), ["common/schema.py", "tests/test_schema_contract.py"])
        for relative, expected in baseline.items():
            self.assertEqual(_sha256(ROOT / relative), expected, f"{relative} was modified")

    def test_lockfile_pinned(self):
        text = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
        for package in ("numpy", "scikit-learn", "matplotlib"):
            self.assertIsNotNone(
                re.search(rf"^{re.escape(package)}==", text, re.M), package
            )
        lowered = text.lower()
        for framework in DL_FRAMEWORKS:
            self.assertNotIn(framework, lowered)

    def test_no_onnxruntime_in_model(self):
        for path in MODEL.glob("*.py"):
            self.assertNotIn(
                "onnxruntime", path.read_text(encoding="utf-8"), path.name
            )

    def test_no_matplotlib_in_init(self):
        imports = _imports(MODEL / "__init__.py")
        self.assertTrue(all("matplotlib" not in name for name in imports))
        tree = ast.parse((MODEL / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual(
            [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))],
            [],
        )


class TestWalkthroughSnippets(unittest.TestCase):
    PATTERN = re.compile(
        r"<!-- snippet-source: (?P<src>[^ ]+) -->\s*```python\n(?P<code>.*?)```",
        re.S,
    )

    def test_walkthrough_snippets_in_sync(self):
        doc = (RESULTS / "KEY_CODE_WALKTHROUGH.md").read_text(encoding="utf-8")
        blocks = list(self.PATTERN.finditer(doc))
        self.assertGreaterEqual(len(blocks), 2)
        for match in blocks:
            source = (ROOT / match.group("src")).read_text(encoding="utf-8")
            snippet = match.group("code")
            self.assertIn(
                snippet.rstrip("\n"),
                source,
                f"snippet drifted from {match.group('src')}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)