import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "79192f747cd838d45b0b363e7560a63554d06d6fccd9a8c06ef6cef3dc51ffb2",
    "a2a-protocol.md": "66f18d60f946b0c98cb4968228c9d18e44aaec5ab18ac0627f7ac2b60b4869d9",
    "acceptance.md": "54ee5fe6a686cf92f801f101a49a848b685300a9f5f9e07ac332c013e73966b9",
    "agent-configuration.md": "c08a9400597d854488e4c8ac2864383b27c0ae70c3efacb5ffc0ea08cdc0ea5d",
    "architecture.md": "9dacc30f3d3a7ec8f228dec240829c05684cafb3742aa07720e107a14669816e",
    "context.md": "c07d21e77d2fc719e4d1e22b4d9583055abbbef4d8ffbbd79f1d0d36d33cde21",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "31fde753c3f962569543febc66203f51949168933c3cd2a2900d315c984f2d3a",
    "implementation-status.md": "aa1e2589860be84a704d3f7b1fe5e60d333c46a4819d6553767ca6ce2759475b",
    "kernel-instructions.md": "c6521b51a7d10bce4e6697c82736bb09d6b75b7ce7ad48fafd0ce18889530454",
    "local-operator-hitl.md": "0b6ae37fb7f062bd83b380e74e580700baee28c9a73118a57e536537ac091e4f",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "a373baa6899d21b3b7a1d9a148e285b3921b4ce1ff658b15927decaf98ed479b",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "e8f464ff5183130f80ec18b0cc5a67b1112401ebdefdfa0644d566ed96ebff1f",
    "releases/v1.md": "c6d0e16314e141022ecee3dae27b0bd9dd06dbb1f183a8e5cf9e6a9024fc5e9b",
    "runtime.md": "c35bf56032682ad2b533af632f3243410f3128d3c26178f134d872fdbf5dc5ec",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "54f97d24c0ef16807b96d4e0bef6e1b3d55b0e3bbbbf395f3f3c5331afc60d25",
    "tools-and-approvals.md": "bf72675e497b05542bf8a18c91b9233d52a08960ea3c51f6d0ce97d85ce9493e",
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
