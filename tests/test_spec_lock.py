import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "89f8728dcc7204bfdf4a513afb763aa20fc6c9767ff7c8db19068a406782b727",
    "acceptance.md": "d701f21b81a10d5cd7ab90ae37b122f3d46b512b6c66a92bd15bea99a5460e6a",
    "agent-configuration.md": "d9b9e1fd72dd2280167d583b67ecb9006b07703956f9bdacf37acf3bba82b659",
    "architecture.md": "62b45026b159815a5494967105d9ce72981fcc5247df16eae0f09477cfec5090",
    "artifacts.md": "c7448130052c9920ee2626e6442032903480f7dfa93dd00bacf1e826c2a27c41",
    "context.md": "fa14117647e7e956834f57fd4a4010c38e2af0285d545c70b688a2883626869e",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "c5d44b35bae8cf94862435cc7074772438412e578816ff9462d1e37f8c453a26",
    "implementation-status.md": "cae44e86d8b08784856eef179b9871d2a395c99abc2378a418f6a732eb2106bb",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "151f1aaa1afd10bdec6f2e18381394e2e3803e3da147af2e3c998517599ab9d9",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "0cc9892b63023fbb11bf5686d31516aaa6185e40b93d0a2f49174868bfecce20",
    "releases/v1.md": "f375afbe2f6c98b6b2414f7681e6fd073287c665c75e76773b332389c98f4ca8",
    "runtime.md": "1cfa4409e565210fad19cf57a61eec050d7fdf795fc66eb4593c81b51856e91f",
    "security-and-reliability.md": "8543de558843ff4faec4fa9069ce9d3923b9c5b7b4864b434768adcb32048906",
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
