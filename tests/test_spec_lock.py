import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "76d28dda482788316b09332608404d8abdb9cfe0e1b41a65adabbba1074868d1",
    "a2a-protocol.md": "97addb24a5ddb02015af2aa1022c36e0d21ddb5bb12ed913522af8ef2f229a3d",
    "acceptance.md": "89c2fc9f5b360d410275dcac965b3e9ee8366fd1e0438f265f4c8f7e48d2f081",
    "agent-configuration.md": "3f125822ba385866c5945a39dba4a28efa30f0200047d6aa5fce28f2741670f4",
    "architecture.md": "c7362d2f0d94099d7d64d9989a52211a9e63c0773390ee248db63c0c2118b9eb",
    "context.md": "c07d21e77d2fc719e4d1e22b4d9583055abbbef4d8ffbbd79f1d0d36d33cde21",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "a65a499d8f822262eb3e9ff5e0d2cb985233131af3579a5754b173631fcea792",
    "kernel-instructions.md": "47055a3deeda3b63cc2020c541d7c0198fd82a1a4b4248c678c18b11d5bc9774",
    "memory-service.md": "239b55fc8b03aed70b55dfd786ada85c6763ed5ddd9b8d999bfb0854a8bcb1b8",
    "observability.md": "8b62458aed0af4dc95f554428d053fc37b21578654d357441e93121c14a9ca4e",
    "product.md": "fa1268d29203f9a5a1a3b7c5730e8d58d51fa1299cc99299032b42cce20ee2a9",
    "public-contract.md": "d63b541d43283d8abdec881a12250f6328c72a0fa488e5cac471ef8a9ae11f83",
    "releases/v1.md": "f1e8b1fa08e0d81fd7a1314bf91a7164c3fd94e3a3c613e010a8a8f7c4b4b0e0",
    "runtime.md": "e49fc9428a63e8f41a22b47630ea7a1e0ecff990330af83981b39ea4715965d2",
    "security-and-reliability.md": "ad0188c0f083e2ab4304ee958f3f3dd898d840ab720f13af87f5ccfb8fb9687f",
    "skills.md": "50d566a6ff8ce28a62bbe57bd376c5a2a76707086a814fd55072b8990e9e9906",
    "tasks-and-delegation.md": "bb722f2cf7353245169efe9e36ec7760b3b42cf63cbd065d865b9e98263a33c9",
    "tools-and-approvals.md": "949a1ef39f664a2de7ccf0b27462c16bf356ab39b7c58735b759e5673ab882b5",
}


class FrozenSpecificationTests(unittest.TestCase):
    def test_specification_files_and_bytes_are_frozen(self):
        actual_files = {
            path.relative_to(SPEC_ROOT).as_posix()
            for path in SPEC_ROOT.rglob("*.md")
        }
        self.assertEqual(actual_files, set(EXPECTED_SHA256))
        for relative, expected in EXPECTED_SHA256.items():
            with self.subTest(file=relative):
                data = (SPEC_ROOT / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
