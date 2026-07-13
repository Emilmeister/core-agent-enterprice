import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "79192f747cd838d45b0b363e7560a63554d06d6fccd9a8c06ef6cef3dc51ffb2",
    "a2a-protocol.md": "66f18d60f946b0c98cb4968228c9d18e44aaec5ab18ac0627f7ac2b60b4869d9",
    "acceptance.md": "71c847d8b61353a5ae35f65864f7975a6048f249a60371ab104aad1d5aa75d54",
    "agent-configuration.md": "8d0cc995abe753c21bdc73c2883b8d4dc2108ee6788b9d6149b219ae655013eb",
    "architecture.md": "9dacc30f3d3a7ec8f228dec240829c05684cafb3742aa07720e107a14669816e",
    "context.md": "c07d21e77d2fc719e4d1e22b4d9583055abbbef4d8ffbbd79f1d0d36d33cde21",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "41d1480f00311afb6a31dc14cd1496a78f8b4eab0bcac019c6452e2ab0efbd28",
    "implementation-status.md": "fedbf6ca644d1b7ad1ee7cc83f65bb1efd970d33324557839ab9d6cdf33c18e2",
    "kernel-instructions.md": "8470ab3d96ca5a88ad12ea10b3b5c3cbf6451c72f6f4bcc2611556359687ca9f",
    "local-operator-hitl.md": "0b6ae37fb7f062bd83b380e74e580700baee28c9a73118a57e536537ac091e4f",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "a373baa6899d21b3b7a1d9a148e285b3921b4ce1ff658b15927decaf98ed479b",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "e8f464ff5183130f80ec18b0cc5a67b1112401ebdefdfa0644d566ed96ebff1f",
    "releases/v1.md": "c6d0e16314e141022ecee3dae27b0bd9dd06dbb1f183a8e5cf9e6a9024fc5e9b",
    "runtime.md": "c35bf56032682ad2b533af632f3243410f3128d3c26178f134d872fdbf5dc5ec",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "17557cb26dbd9cecfe03b0b65a275ecae5f146719dd89ff347d586abde2ca308",
    "tools-and-approvals.md": "2cc1869e93d553781ef2c8f974a38bfd95c7da3e25bb6f89029dbf81e0c004e6",
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
