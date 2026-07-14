import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "8b7b84a90cff245c55fe32e206f723cb4aa13b00d42b01cf5a5dcec2dbef0515",
    "a2a-protocol.md": "1e5a54ec48f088bca4f027d327a3d479f9be495273acdfba73ec2dc9e49bba23",
    "acceptance.md": "4a9271fd12b93220423556562c7910c2613e46a79d4c6d89c018cb45b4332cae",
    "agent-configuration.md": "5f9123a8b997e0f849dde5b9e4f62d4b3ea9d812dae5e583ba6b9b99030ef502",
    "architecture.md": "065c1640d2dce80f74ace9bbb173af25f246c802ddb3b3079e9b364ba4635de7",
    "context.md": "a995bd55865f699ad7f1271351989bc1edd99f0949fa2e3329cc6504c6362d30",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "41d1480f00311afb6a31dc14cd1496a78f8b4eab0bcac019c6452e2ab0efbd28",
    "implementation-status.md": "211e9b423f67bd8bf2303c9401fc59c038c278d30c7c6ab9d89eac2703033e70",
    "kernel-instructions.md": "8470ab3d96ca5a88ad12ea10b3b5c3cbf6451c72f6f4bcc2611556359687ca9f",
    "local-operator-hitl.md": "b49f326296f1a846c9e04d3503db1392798604ccba2605ab3ed9d859badcbad7",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "a0db93b68be416a723440d292f9f96dec43e6a059bfa2dc51b6825f10f85f4ba",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "9f293befcd0c084b67104e1418f5683364fed22e4d1c71d3e8f6c5d065343b39",
    "releases/v1.md": "e974143bd2836de1067a2c92e96100555665bfc476029cab8ea0d88e53d509aa",
    "runtime.md": "9776e0530b6105591759c4dad72ab75acd359705aa4c67cfea58304a84dc6dca",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "f3fea2bdaa7b5a821cfdcc1c62b78325c4be4e98d5270cd78d0d2c521cc6e9e6",
    "tools-and-approvals.md": "78e83d00c52ecafc6c118f278ab6f45dd248f58241baab8858977a63e456e460",
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
