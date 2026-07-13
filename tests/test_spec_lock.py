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
    "implementation-status.md": "bb9561c3cfc32e716526cacf1f737d56e54eed43f3c98fa681f05392a1aeb948",
    "kernel-instructions.md": "c6521b51a7d10bce4e6697c82736bb09d6b75b7ce7ad48fafd0ce18889530454",
    "local-operator-hitl.md": "0b6ae37fb7f062bd83b380e74e580700baee28c9a73118a57e536537ac091e4f",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "efcf61e49f8c03084378c565a92752e6389830736bfe57ad8411b46f50be1bb1",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "e8f464ff5183130f80ec18b0cc5a67b1112401ebdefdfa0644d566ed96ebff1f",
    "releases/v1.md": "c6d0e16314e141022ecee3dae27b0bd9dd06dbb1f183a8e5cf9e6a9024fc5e9b",
    "runtime.md": "ebba00d9ff7d752a8aea5bf431cd9c25c00fa6a545c5cbb72b1c02dcc93b07d3",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "9ec3b3ff94d1a5d475848f6393f09b077f5f7bcc9264a2aa21635bba5a27d27a",
    "tools-and-approvals.md": "c6cc3aedd47fe13494889b2922edc6ed2564d0a201e8bb1cf78fce081d5e230f",
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
