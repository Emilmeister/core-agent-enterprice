import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "6909ad0800ffd1d4bbda6cbfe5a9b75a6a060c7df72c843de357a8fac4e61ff6",
    "acceptance.md": "d72f263314d68402074a6c697aa079291f4ec17475c1e0e683fd2a3cfa3dd5a3",
    "agent-configuration.md": "31da477123dadcbf2d5915eb198860c55e24e204af0fed9ba1d67af2a28d199f",
    "architecture.md": "62b45026b159815a5494967105d9ce72981fcc5247df16eae0f09477cfec5090",
    "artifacts.md": "8e9b9a8523542c515ef27b63eccfba3ef1ee56f43b7892e831878405e1ce69b2",
    "context.md": "eceb11cbc60fb041f13df987f834ea65937b6de01e0ca444b18951892c694f14",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "1ad7324e418abbb4861c132422ce48bf995508c93dbb12ccc04ac30fd05bb681",
    "implementation-status.md": "a1c6575f258cb6c8b1748350c410594899793e61c364775606c45d63713d1f81",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "d96324b2c4cb9d06c9f1d93148a5cfdc5c8b70a34689c1fed668609a8b11b8bb",
    "releases/v1.md": "e277d75af28253f2e237181a9d10c141acf22527a25f9bd1d83737594c59021d",
    "runtime.md": "37cb36ddef353bc270691b84069a5619c54da396c1dfcdf28f0bda984200f259",
    "security-and-reliability.md": "6d99b220bb9387a4313b0006b83980f8ab57456d1b75e3e566d6394bde5a38a7",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "06a79f95dbe091263fcb620fa5aafc2a67edbc2e8ab1d183c21e87ad732a29a9",
    "tools.md": "8d0d5846374e04dfa6184024c4ec8043643694274c2f1611290f6216dd4284ab",
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
