import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "8b7b84a90cff245c55fe32e206f723cb4aa13b00d42b01cf5a5dcec2dbef0515",
    "a2a-protocol.md": "1e5a54ec48f088bca4f027d327a3d479f9be495273acdfba73ec2dc9e49bba23",
    "acceptance.md": "fa415be31edd72c5fa9c1f0c95adf8f72b72add75960a2780187dc77e5f45ed7",
    "agent-configuration.md": "db123d34cf78007faef54730cbed3d28c5fb58be173c5b062d740291667ed78d",
    "architecture.md": "065c1640d2dce80f74ace9bbb173af25f246c802ddb3b3079e9b364ba4635de7",
    "context.md": "a995bd55865f699ad7f1271351989bc1edd99f0949fa2e3329cc6504c6362d30",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "41d1480f00311afb6a31dc14cd1496a78f8b4eab0bcac019c6452e2ab0efbd28",
    "implementation-status.md": "0f01a6df8558dc1669b7c5c2710ee9e9dca0b3c220c5a34dd4727c701e45ef1f",
    "kernel-instructions.md": "8470ab3d96ca5a88ad12ea10b3b5c3cbf6451c72f6f4bcc2611556359687ca9f",
    "local-operator-hitl.md": "b49f326296f1a846c9e04d3503db1392798604ccba2605ab3ed9d859badcbad7",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "65f5ca28757f067d07931c858cabe7c0d882275170288d4cfa47c879972ad120",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "9f293befcd0c084b67104e1418f5683364fed22e4d1c71d3e8f6c5d065343b39",
    "releases/v1.md": "5d79d5d2c8a40dca4236e2e978da5c290b6391e468516c37dcb2b6390fb0b0b6",
    "runtime.md": "cb78c26469a7c835f920ff0da8a6c9f96877bba90e4ef200fe35d22a76ec564c",
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
