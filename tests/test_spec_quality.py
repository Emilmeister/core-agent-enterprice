import json
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"


class SpecificationQualityTests(unittest.TestCase):
    def test_repository_contains_no_committed_private_key_or_api_token(self):
        patterns = (
            re.compile(r"sk-[A-Za-z0-9_-]{32,}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        )
        roots = (ROOT / "core_agent", ROOT / "tests")
        files = [ROOT / ".env.example", ROOT / "README.md"]
        for root in roots:
            files.extend(root.rglob("*.py"))
        for document in files:
            content = document.read_text(encoding="utf-8")
            for pattern in patterns:
                with self.subTest(document=document, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(content))

    def test_relative_markdown_links_exist(self):
        pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        for document in SPEC.rglob("*.md"):
            for target in pattern.findall(document.read_text(encoding="utf-8")):
                target = target.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(document=document.name, target=target):
                    self.assertTrue((document.parent / target).resolve().exists())

    def test_headings_are_unique_within_each_document(self):
        for document in SPEC.rglob("*.md"):
            headings = [
                line.strip()
                for line in document.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            ]
            with self.subTest(document=document.name):
                self.assertEqual(len(headings), len(set(headings)))

    def test_json_and_yaml_examples_parse(self):
        fence = re.compile(r"```(json|yaml)\n(.*?)```", re.DOTALL)
        for document in SPEC.rglob("*.md"):
            for language, value in fence.findall(document.read_text(encoding="utf-8")):
                with self.subTest(document=document.name, language=language):
                    parsed = (
                        [json.loads(value)]
                        if language == "json"
                        else list(yaml.safe_load_all(value))
                    )
                    self.assertTrue(parsed)
                    self.assertTrue(any(document is not None for document in parsed))

    def test_release_profiles_have_required_sections(self):
        required = {
            "## Что входит",
            "## Что не входит в этот profile",
            "## Критерии приёмки v1",
            "## Exit criteria",
        }
        for document in (SPEC / "releases").glob("*.md"):
            headings = set(
                line.strip()
                for line in document.read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")
            )
            with self.subTest(document=document.name):
                self.assertTrue(required <= headings)


if __name__ == "__main__":
    unittest.main()
