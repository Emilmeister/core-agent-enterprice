import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "b49c132e19b067cf0b00541fe058c505edd2b99f5c8dd6205854fe2b60e93df7",
    "a2a-protocol.md": "97addb24a5ddb02015af2aa1022c36e0d21ddb5bb12ed913522af8ef2f229a3d",
    "acceptance.md": "108825858e44798513b9bcd1037c645635d3f80607088bae0abe2e32fbc141c7",
    "agent-configuration.md": "980e69d713a3752f19181c55a82384843200911115ff544451d31f14edc3617b",
    "architecture.md": "07a09535cf5a0385e873dba65376cd96ee2465337b69f8e6cb326cbd73894625",
    "context.md": "c07d21e77d2fc719e4d1e22b4d9583055abbbef4d8ffbbd79f1d0d36d33cde21",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "e9614ae9596c9e39a6b1a235e5ca50e17cbee10bb2855c71b0907e2016b4096e",
    "kernel-instructions.md": "37b2b9f0940c909c75d09d451737f511897cc2d2ed8e46675ea6bc95dc0112b7",
    "memory-service.md": "239b55fc8b03aed70b55dfd786ada85c6763ed5ddd9b8d999bfb0854a8bcb1b8",
    "observability.md": "21f498a5ea4652bd20ac9c2a3b7c9055cb24eed4eb25ee7f95d72eb64c59299f",
    "product.md": "4722378a59ab5fee77b38d2eba9a86efa7feb2d5b8721e1f499f1677790099c3",
    "public-contract.md": "dd468d349920fd829be62407f095157070267c56dec75309d88de8e3a9a92a0f",
    "releases/v1.md": "6df535c551f7e44f16e160ace2e663f65ee62ee9fe81f1db85177d1ff7cbf33a",
    "runtime.md": "b9581c4067be45e64c71023da57141bfd15f2008aeaa92fdb82c96f470bc94da",
    "security-and-reliability.md": "78438365e7f140c7372964f37070f37e7a75d8efb66a6251277740129c955df7",
    "skills.md": "7102b91a2f574ea5b75c02688a14ed5da7bbafc7d19b66b159330eae09600426",
    "tasks-and-delegation.md": "123b5f1b94c8e0ed5c3b92e3f0109b8c9a31a15ed9c258f8a26edb2568c0ce10",
    "tools-and-approvals.md": "fd08702f7dd88f715e22899025585072be3f1b46bbd180126504dccee9d7992b",
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
