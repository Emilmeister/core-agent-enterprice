import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "d7742805d1e460e3cf72d963bdff04b41d2920bbb75272e01a7be6207dc7db99",
    "a2a-protocol.md": "0354e42a1db2c34db7ff6422049aeb16a3fd6fa6aa5b46c6ae30657c54112202",
    "acceptance.md": "9713aa56a3ef9f9d9a154aa689c801bf13ee4c6bceb0662fd687bb3d337ce184",
    "agent-configuration.md": "ea98c015032bcc61b235a138593c248c9d90aa7d028bd1547e1d3c8b59e3cc2f",
    "architecture.md": "081f8ca7d0319b7e838618972d1023659bf2450e0ce12bdcb6570af9cef29c28",
    "artifacts.md": "ad7b39b54bb0c4194fb577d361499c7dc091f6a9022d499b507e7e0569fb909c",
    "context.md": "bf215276abb5b9a14829c486e6de833ba4662ab2be57883569e6f434a5b1f7ad",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "be50b6958275d13563ca848b5a42dc7562082f09417ed34f619a0da3ff3eccf5",
    "implementation-status.md": "ccd64636912cea72c32b97c38b52f86f057e7c696df8fb24dabe9fa6c66852c9",
    "kernel-instructions.md": "de0cf96432e9adfacf1d9a7bebbc3f0879373d274b6627f51d944915ce677ed6",
    "memory-service.md": "4b69874bf3f2cb6ae11fcad1d427db9ac0eef4cc83cdd516367e1c2ec3c9e33f",
    "observability.md": "975aa10b2b97287efb1cdb2c5f6b1b8b834bcab95bb6aa133f2f39b7090223d9",
    "product.md": "2cf9425df53fffc19b10eb5f74d6baed4b55343e2ccedefb376db54c517994ee",
    "public-contract.md": "c4bcbe8cbc8944f73c4a0259c06a770dce98f41217b4bc5bf7fe41eec0018941",
    "releases/v1.md": "bb23d692c08ab7b75f0510fdd57dc6d104882c6b3e503b6185aa5a3f35a4e18b",
    "runtime.md": "09a1fc319ced97c82d8fc16dd5c43cdb5afbc143f68e5c2d05f810e6382224b6",
    "security-and-reliability.md": "51968f107e23a41da86293ec948817282915c490761097e2584b6a46773b62c4",
    "skills.md": "5a1a73c1ea3db1ddd4403534f49c04b2a83f1378b6199eb5be9c7b254b4202df",
    "tasks-and-delegation.md": "9a0e83e914996a016e22e36f0fd34d720022b2eaac277c31d8bc8d795cc4dee9",
    "tools.md": "f9f552fc79b6212dc09516b4b68f6fba41b6ffd6ee85fde1a319ccc4f19b635d",
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
