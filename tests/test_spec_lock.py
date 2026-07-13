import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "79192f747cd838d45b0b363e7560a63554d06d6fccd9a8c06ef6cef3dc51ffb2",
    "a2a-protocol.md": "66f18d60f946b0c98cb4968228c9d18e44aaec5ab18ac0627f7ac2b60b4869d9",
    "acceptance.md": "2883640dd7d1d039b55f6f396b8fb550498aa5d7b6002406a2166fa86e482190",
    "agent-configuration.md": "5d65f993f7a495808f09e6e6d709f8f30a5e245a1bc5d1b8688f348625989010",
    "architecture.md": "9dacc30f3d3a7ec8f228dec240829c05684cafb3742aa07720e107a14669816e",
    "context.md": "c07d21e77d2fc719e4d1e22b4d9583055abbbef4d8ffbbd79f1d0d36d33cde21",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "e9614ae9596c9e39a6b1a235e5ca50e17cbee10bb2855c71b0907e2016b4096e",
    "implementation-status.md": "60d95bb44e6bb59d327a7b263deece3de62512c075b8b57351d001dfa6a73e78",
    "kernel-instructions.md": "37b2b9f0940c909c75d09d451737f511897cc2d2ed8e46675ea6bc95dc0112b7",
    "local-operator-hitl.md": "0b6ae37fb7f062bd83b380e74e580700baee28c9a73118a57e536537ac091e4f",
    "memory-service.md": "176f9de2353423d742f04d256affdcb919967274e3509ae32f1f1dd16cfb204d",
    "observability.md": "21f498a5ea4652bd20ac9c2a3b7c9055cb24eed4eb25ee7f95d72eb64c59299f",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "e8f464ff5183130f80ec18b0cc5a67b1112401ebdefdfa0644d566ed96ebff1f",
    "releases/v1.md": "22f9a75fb6b312b1f69b799e2d1701905125979cf13a8d7c3a3ee0c84beed060",
    "runtime.md": "af4e8cabd3ece77e47c92f2205d7166ac456889e86881429a3c28b6e7b011237",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "ca7278aa05d6b5137de0ddaa7bc9cd127733dfd9cd5dd0f929833b02c9a1d981",
    "tools-and-approvals.md": "221ef341680f16d4ca7794311f92c51f0e91183abfadd8de9cc902b71cd1915c",
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
