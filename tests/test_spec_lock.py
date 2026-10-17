import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "89f8728dcc7204bfdf4a513afb763aa20fc6c9767ff7c8db19068a406782b727",
    "acceptance.md": "5b2ef2f99de2c8d846ae25dd3630a0158b79f8c245f266b4eb1c12f59a8f4faf",
    "agent-configuration.md": "65ee5aab7a7991e4c5f3ac986dd5713360c47731ae88bf3f0d0916bad77646e9",
    "architecture.md": "62b45026b159815a5494967105d9ce72981fcc5247df16eae0f09477cfec5090",
    "artifacts.md": "c7448130052c9920ee2626e6442032903480f7dfa93dd00bacf1e826c2a27c41",
    "context.md": "eceb11cbc60fb041f13df987f834ea65937b6de01e0ca444b18951892c694f14",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "c5d44b35bae8cf94862435cc7074772438412e578816ff9462d1e37f8c453a26",
    "implementation-status.md": "029c0f0a4b6fc17b4bcfcc5e0f4a898b9238584a54e5cad229c2da556cdc0962",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "151f1aaa1afd10bdec6f2e18381394e2e3803e3da147af2e3c998517599ab9d9",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "0cc9892b63023fbb11bf5686d31516aaa6185e40b93d0a2f49174868bfecce20",
    "releases/v1.md": "757f5a203701a86bc8242a57cb668bbe6155d3f4dadd1c95e9567e02c3104032",
    "runtime.md": "1cfa4409e565210fad19cf57a61eec050d7fdf795fc66eb4593c81b51856e91f",
    "security-and-reliability.md": "6d99b220bb9387a4313b0006b83980f8ab57456d1b75e3e566d6394bde5a38a7",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "dc3ef867de888e1f5e8b6ba0d47deeead4006aca91cb5ec6215b369569f8167e",
    "tools.md": "33af398a7340c309f8dc13bf397e515f102079b38ebe94d346179c2e1bd2c96b",
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
