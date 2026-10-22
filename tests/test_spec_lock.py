import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "e66378d221228211b264d5c63b7923a3802b9007f9450687d560c0c01cf14503",
    "a2a-protocol.md": "5852a57d8b8120f4e0e4057ad50caea188fb43d60b45c3019ed9fc35c71d4a44",
    "acceptance.md": "008df75d3ae5cb55126dec2c2dfe66a27245793e50ef3c3642e14382ac386371",
    "agent-configuration.md": "a201ec88e632be47a1f069f53ff47b7d9441091293d0cf13c1d3f94e4399b33f",
    "architecture.md": "e455a067f6be33932ae35149ec8be522e89bc939c84764c1c94ae0aac8c04f37",
    "artifacts.md": "188d1b2e5bc5dc19546ca504b5f4ea40824713358db033fa3adef8e8df2122cb",
    "cloudru.md": "a50aa61446c4f8ff05eb69588c9c8a4031fc3a33f06ecc267b85a315d9b20d21",
    "context.md": "a50004c1a15f228ed758a4b965be1a3fb7953f6069372acfd8216f181cc7f2cd",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "bfe8cae410228775d2372d42dfdf99f5a11c36ec882beb60a61cdbe55b5aa24a",
    "implementation-status.md": "305f548201949775d1fc5e949cc95a2596a1d8fd304f5396ddc0868e83a19659",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "151f1aaa1afd10bdec6f2e18381394e2e3803e3da147af2e3c998517599ab9d9",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "b52c758f80a0bd095fa655735d78816333ce6cd97c10cb8fd7691d330a2422dc",
    "releases/v1.md": "a7442bab7741c864387be1f655b5027bc5425d2f8d0fed8403b08807a7623b9a",
    "runtime.md": "f7242db032b2aab8ecb2f93b54248e0acbcb15ee8e4b6245357cfe1da9ca3e5c",
    "security-and-reliability.md": "8a1d2ef152587cfdf086f8c900a91fd9808fd0c6b8d606bb35d555c01aadd9c2",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "c59f4c923a5a34e00e34fb413920a93cb393e7d821ed74731a4d3ae532f6a408",
    "tools.md": "a5f9eb6e85f8c46d8b24b6226ba532b2c8569cfab80c40f53a755e8c04b40adb",
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
