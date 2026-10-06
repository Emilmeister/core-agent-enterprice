import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "ffcbe0c15005a5469fe196e38617ce8dcd04128ed74d6eb7db1939206764c9b0",
    "acceptance.md": "d1b18697b1681a274183a11540518161577185523357ef0b395e3efb80222c42",
    "agent-configuration.md": "31da477123dadcbf2d5915eb198860c55e24e204af0fed9ba1d67af2a28d199f",
    "architecture.md": "62b45026b159815a5494967105d9ce72981fcc5247df16eae0f09477cfec5090",
    "artifacts.md": "0a8d75aaf47279a0148954739cffe888e0ae5208c67356d5145cbde2c2045487",
    "context.md": "eceb11cbc60fb041f13df987f834ea65937b6de01e0ca444b18951892c694f14",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "1ad7324e418abbb4861c132422ce48bf995508c93dbb12ccc04ac30fd05bb681",
    "implementation-status.md": "24da1ebcd3c86ff7c8bc3683c65301a744ece672f3473dfea065c18cc520987c",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "e8873159e78e3a03849189c710afbe4fd7aa70fc1a9f7ad1f4db59fcbf41a5e6",
    "releases/v1.md": "813fbd0ed06b947a75d83d16bff40c37dc3e973dbe135b7bb1649f4fa82f6073",
    "runtime.md": "bfebefdb71cc4a9a402ef0ae3b537f17d81f784eb655ed05a8469087946a8f7d",
    "security-and-reliability.md": "6d99b220bb9387a4313b0006b83980f8ab57456d1b75e3e566d6394bde5a38a7",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "6d81e0dd3c8b006f7b774dee9b9b3a3c9ac27c5d6f3804071e533790c451deb9",
    "tools.md": "1c8d318469693ca4237eef772c9794244762154a3375e43f3be910de547de93d",
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
