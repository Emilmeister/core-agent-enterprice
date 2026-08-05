import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "d7742805d1e460e3cf72d963bdff04b41d2920bbb75272e01a7be6207dc7db99",
    "a2a-protocol.md": "318dacad97c33a6d4db6e0d24e81f644459788a78c837e43be45b12968ba1413",
    "acceptance.md": "c15c77b3d22641076c98d1209df329bca1733cdb59c94eda7d1fcf5447026240",
    "agent-configuration.md": "55e50b4dd33173ce79f757ca1508507d62498432b78239ee1854ac07cf1fbc8f",
    "architecture.md": "b3f2ef496b325a7c54e3ba711bfaf11ed7c0d7b44e90423d78d5673b1a23ac6b",
    "artifacts.md": "ad7b39b54bb0c4194fb577d361499c7dc091f6a9022d499b507e7e0569fb909c",
    "context.md": "8f9a3b5f4c6a22a889bcab974cd7979d90ef33c1d171ad96b19b2f85557e8e56",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "be50b6958275d13563ca848b5a42dc7562082f09417ed34f619a0da3ff3eccf5",
    "implementation-status.md": "6a01cb0158c4c798c8e2884c541075cb8b0000a1ae9a3c2ba2c2635ef82bf74c",
    "kernel-instructions.md": "de0cf96432e9adfacf1d9a7bebbc3f0879373d274b6627f51d944915ce677ed6",
    "memory-service.md": "4b69874bf3f2cb6ae11fcad1d427db9ac0eef4cc83cdd516367e1c2ec3c9e33f",
    "observability.md": "975aa10b2b97287efb1cdb2c5f6b1b8b834bcab95bb6aa133f2f39b7090223d9",
    "product.md": "2cf9425df53fffc19b10eb5f74d6baed4b55343e2ccedefb376db54c517994ee",
    "public-contract.md": "551ccdc45aeb15f1a4b11a3cf5952a7d6d829a5e2e752f15e25f902e3d41ecbe",
    "releases/v1.md": "2795f464798039ffb474461d9873b78bee161810ea881e9e65e295fd7c3ce9b7",
    "runtime.md": "4efe5fcea4e43485a4954cb152f17ba94aacc86f54dcc192f90b43bc18a0b95d",
    "security-and-reliability.md": "735fb37640bbbc3915ba7a709e79b6cfcfdb80847dbad8b4439d07c5cef3fb37",
    "skills.md": "86acd6b2b648f5fff11f26bd360c8b0a71a874a9a541003618d10379392e681a",
    "tasks-and-delegation.md": "156c15a3867bb5ba8dd1e2c689c83e6881968c15202e8597fc7492ea920f4346",
    "tools.md": "569f835daa393aed1f8c34800f1baf6779a3e5e27e4f3d96a0717772e577bfe9",
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
