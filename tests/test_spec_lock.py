import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "89f8728dcc7204bfdf4a513afb763aa20fc6c9767ff7c8db19068a406782b727",
    "acceptance.md": "d1aa62e72dc898057401bd4a51446ce5d0b970f5de277912f573a4a92578e323",
    "agent-configuration.md": "be342cbf5a41e6c885da56db85467b2299d5a703bbc8721850374c3ce356c2ef",
    "architecture.md": "62b45026b159815a5494967105d9ce72981fcc5247df16eae0f09477cfec5090",
    "artifacts.md": "c7448130052c9920ee2626e6442032903480f7dfa93dd00bacf1e826c2a27c41",
    "context.md": "a50004c1a15f228ed758a4b965be1a3fb7953f6069372acfd8216f181cc7f2cd",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "c5d44b35bae8cf94862435cc7074772438412e578816ff9462d1e37f8c453a26",
    "implementation-status.md": "94013f9e5a65bf32112f70430b383ff94de45bdf541ea9c441b83fd4cbe4f37e",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "151f1aaa1afd10bdec6f2e18381394e2e3803e3da147af2e3c998517599ab9d9",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "133060599e065a372edbda8020548883a67c26fe1fbf6aac2bcab4ad6923407f",
    "releases/v1.md": "8481e3104db9ec14ecd1c68e30dde0a244f9eabf19d8bdf00ec48169bcda2841",
    "runtime.md": "50f11b4f5c047934f8d221190d83a059e036b84544a2730e9b769222fcf7872e",
    "security-and-reliability.md": "b109f8190e0c289fb55718587d3e0bfc9ddf5a1fb3cc6c508c2dfe6314cdba8b",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "c59f4c923a5a34e00e34fb413920a93cb393e7d821ed74731a4d3ae532f6a408",
    "tools.md": "00c659ec3953daca683c0b0279bfc5cf7efe97090eff11fb6ce100ea09441291",
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
