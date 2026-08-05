import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "d7742805d1e460e3cf72d963bdff04b41d2920bbb75272e01a7be6207dc7db99",
    "a2a-protocol.md": "956eb86dcf1c8ec2cf3afc2eb4c02fe6542ef800226ac526736360306db9b2a8",
    "acceptance.md": "4f0c8e0f5229066a3b0c2088e5c4a10916c92e25ddc975c25d562c7ba7b95851",
    "agent-configuration.md": "55e50b4dd33173ce79f757ca1508507d62498432b78239ee1854ac07cf1fbc8f",
    "architecture.md": "b3f2ef496b325a7c54e3ba711bfaf11ed7c0d7b44e90423d78d5673b1a23ac6b",
    "artifacts.md": "ad7b39b54bb0c4194fb577d361499c7dc091f6a9022d499b507e7e0569fb909c",
    "context.md": "8f9a3b5f4c6a22a889bcab974cd7979d90ef33c1d171ad96b19b2f85557e8e56",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "be50b6958275d13563ca848b5a42dc7562082f09417ed34f619a0da3ff3eccf5",
    "implementation-status.md": "66251751199db07756b990e9becb400365227334caebe8c1494779325b511690",
    "kernel-instructions.md": "de0cf96432e9adfacf1d9a7bebbc3f0879373d274b6627f51d944915ce677ed6",
    "memory-service.md": "4b69874bf3f2cb6ae11fcad1d427db9ac0eef4cc83cdd516367e1c2ec3c9e33f",
    "observability.md": "975aa10b2b97287efb1cdb2c5f6b1b8b834bcab95bb6aa133f2f39b7090223d9",
    "product.md": "2cf9425df53fffc19b10eb5f74d6baed4b55343e2ccedefb376db54c517994ee",
    "public-contract.md": "7f4a505f58a8225b8c7128b6e56cd5545a14a625bf02a8c51f7f02e439c32690",
    "releases/v1.md": "d9c40919e91112f56437c5bc882a649d1b9caaa0af819ab9ea308c8e9ecd6c64",
    "runtime.md": "0774a2469770637fcedf9168e07f494493e2084ee9e5df3a2577960e5cd804cc",
    "security-and-reliability.md": "735fb37640bbbc3915ba7a709e79b6cfcfdb80847dbad8b4439d07c5cef3fb37",
    "skills.md": "86acd6b2b648f5fff11f26bd360c8b0a71a874a9a541003618d10379392e681a",
    "tasks-and-delegation.md": "9a0e83e914996a016e22e36f0fd34d720022b2eaac277c31d8bc8d795cc4dee9",
    "tools.md": "4ba47db778898de9d65bf8ed161730b081824b42bdbb60aa2d63501238f67e37",
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
