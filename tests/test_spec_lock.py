import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "3fbd47e36fe7f2ee3830afec0ec86f929fa06ee85d20581ef111402845449786",
    "a2a-protocol.md": "b7b8d4eb7167d98ae9e5713b18575e85708676506118a7845492ebcd2e5d565e",
    "acceptance.md": "e82badc9ac752a9ed561da4a8a4018ab8c42492b362f0ecab146284c816b488c",
    "agent-configuration.md": "63ce3fe39cbb1251a4373a8c010806e35af066f6791eeab9f979bcc60baecf01",
    "architecture.md": "e455a067f6be33932ae35149ec8be522e89bc939c84764c1c94ae0aac8c04f37",
    "artifacts.md": "93112c7f43a7823aa6bc4607d0215006a287a2fae120cdd3b2e4ca235fd2f8d3",
    "cloudru.md": "011acba6a2d1cabab87e9b3c9d85025c1905756bf36c422c456b092a2b675b3b",
    "context.md": "775883c92d5365a6476d2dab38e7428b8f5c1c1d98d67c0a7e6f20306f83d23d",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "4b2f780d7689a6e47d53ac141c557b8ecf1d144d2f73e4d8d8d472e4dfae9077",
    "implementation-status.md": "d33995a3b48a546b63861ce942b99b36bbaa164662467d032d91de65359532b7",
    "kernel-instructions.md": "46b3c0b04bf23a23e88be443f75c721a3cee6aa220da38e6f330e12b59b1c932",
    "memory-service.md": "fb2381809c31bbf8679bb2fbeda7d6f69b1f7db37584a85b3d39af5ec0b23a27",
    "observability.md": "93fcee73dbde5819ed8ceb0c01ff92455a7301df9d8f45f5a94d96cbe21f12ae",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "53bcedc9bf378aa1a802396da4576fd51f6f67fb9ed1c5529a054ac54bcee3a1",
    "releases/v1.md": "9dcb777bb2d96a2c657abbc2a3fe02a3d589855f4c99d6d2741281da9ec3c3c6",
    "runtime.md": "dcd5095955fd885e4ca33294c5ddae8bd40d1d9e1879b28cff487b7edb0cd03c",
    "security-and-reliability.md": "9673cd15c0add43c35f79d82fb9aba838170892f2c5149ded0df998893e3d4f5",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "9b732377a7163136e542425b2b607aa51bb23f8b6c459de6ea6404efef2e2907",
    "tools.md": "f34d7966a1aab37138ef1cd20e37a0d9148cdbe2698eb078a4115c06869ac312",
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
