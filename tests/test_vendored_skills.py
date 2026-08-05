import hashlib
import unittest
from pathlib import Path

from core_agent.skills import SkillResolver


ROOT = Path(__file__).resolve().parents[1] / "third_party" / "skills"
EXPECTED = {
    "systematic-debugging",
    "verification-before-completion",
    "knowledge-synthesis",
    "explore-data",
    "validate-data",
    "statistical-analysis",
    "sql-queries",
}
APACHE_ADAPTED = EXPECTED - {
    "systematic-debugging",
    "verification-before-completion",
}


class VendoredSkillTests(unittest.TestCase):
    def test_manifest_and_packages_are_complete_and_loadable(self):
        packages = {path.name for path in ROOT.iterdir() if (path / "SKILL.md").is_file()}
        self.assertEqual(packages, EXPECTED)
        self.assertFalse(any(path.is_symlink() for path in ROOT.rglob("*")))

        manifest = {}
        for line in (ROOT / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            digest, relative = line.split("  ./", 1)
            manifest[relative] = digest
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        }
        self.assertEqual(set(manifest), actual)
        for relative, expected in manifest.items():
            with self.subTest(file=relative):
                self.assertEqual(
                    hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(),
                    expected,
                )

        resolver = SkillResolver(
            [
                {"name": name, "source": (ROOT / name).as_uri()}
                for name in sorted(EXPECTED)
            ]
        )
        self.assertEqual({skill.name for skill in resolver.discover()}, EXPECTED)
        self.assertEqual(set(resolver.resolve_lock()), EXPECTED)
        for name in EXPECTED:
            skill = resolver.activate(name)
            self.assertTrue(skill.instructions.strip())
            self.assertEqual(skill.resources, resolver.list_resources(name))

    def test_adapted_apache_skills_identify_the_local_modification(self):
        for name in APACHE_ADAPTED:
            with self.subTest(skill=name):
                frontmatter = (ROOT / name / "SKILL.md").read_text(
                    encoding="utf-8"
                ).split("---", 2)[1]
                self.assertIn(
                    "modified: Adapted for Core Agent; differs from the pinned upstream version.",
                    frontmatter,
                )


if __name__ == "__main__":
    unittest.main()
