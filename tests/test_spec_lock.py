import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "d599ea4bddbd0ccba15aa353bddf055d8bb909cd67fe4735e24b1ac320676f6f",
    "a2a-protocol.md": "4a8dfac42072c49600db742cb679286d8d6a4562ada4528dd0df23921cd7c9ea",
    "acceptance.md": "bff89f611138ec68ce05ed49fe87c42e222f5f8de5726660b002488975a8691e",
    "agent-configuration.md": "50a6be358f5ec06d9bde7720bd837c0381b61b1cfb8cee0b8533550ba30b13b1",
    "architecture.md": "e455a067f6be33932ae35149ec8be522e89bc939c84764c1c94ae0aac8c04f37",
    "artifacts.md": "188d1b2e5bc5dc19546ca504b5f4ea40824713358db033fa3adef8e8df2122cb",
    "cloudru.md": "cea2cb9b525b869dbf5fb30e4d313941ec377cdc58e1282f67dcbd0b31f71b02",
    "context.md": "a50004c1a15f228ed758a4b965be1a3fb7953f6069372acfd8216f181cc7f2cd",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "f064a7dadd3ee87686fc4fef7e43304cf3ba2b59b4aade69ca5935eea11ecc3c",
    "implementation-status.md": "9edcc664e19912a4e305cb5abee6d259a170746f019361ec673329706061eeca",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "151f1aaa1afd10bdec6f2e18381394e2e3803e3da147af2e3c998517599ab9d9",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "20db1957caae36e81203bebe127cf3d2b238502b1c23c47506df31da27d05ffa",
    "releases/v1.md": "32b32433bab500708f1bbc5de08e998716dfe5f3be4dec3349d70041315d3e3d",
    "runtime.md": "f7242db032b2aab8ecb2f93b54248e0acbcb15ee8e4b6245357cfe1da9ca3e5c",
    "security-and-reliability.md": "767cb6ab5e1a710f15036a202dc577d02514e46fcd16e3614a9e5866f92cbbad",
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
