import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "d7742805d1e460e3cf72d963bdff04b41d2920bbb75272e01a7be6207dc7db99",
    "a2a-protocol.md": "0354e42a1db2c34db7ff6422049aeb16a3fd6fa6aa5b46c6ae30657c54112202",
    "acceptance.md": "04c32a4b76fb7fda6d8d1821eee43618bc92f67632aa0daf7580e4d0d6070442",
    "agent-configuration.md": "3b8168a50b5efa282cab8b6e6d6d0f25a431c8ef7909902f8460f8eaf6d47bae",
    "architecture.md": "081f8ca7d0319b7e838618972d1023659bf2450e0ce12bdcb6570af9cef29c28",
    "artifacts.md": "ad7b39b54bb0c4194fb577d361499c7dc091f6a9022d499b507e7e0569fb909c",
    "context.md": "8f9a3b5f4c6a22a889bcab974cd7979d90ef33c1d171ad96b19b2f85557e8e56",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "be50b6958275d13563ca848b5a42dc7562082f09417ed34f619a0da3ff3eccf5",
    "implementation-status.md": "b91016096b41ae9988461a3617b6d760a530bb5065cef9e2f8b444c70a5e064a",
    "kernel-instructions.md": "de0cf96432e9adfacf1d9a7bebbc3f0879373d274b6627f51d944915ce677ed6",
    "memory-service.md": "4b69874bf3f2cb6ae11fcad1d427db9ac0eef4cc83cdd516367e1c2ec3c9e33f",
    "observability.md": "975aa10b2b97287efb1cdb2c5f6b1b8b834bcab95bb6aa133f2f39b7090223d9",
    "product.md": "2cf9425df53fffc19b10eb5f74d6baed4b55343e2ccedefb376db54c517994ee",
    "public-contract.md": "c4bcbe8cbc8944f73c4a0259c06a770dce98f41217b4bc5bf7fe41eec0018941",
    "releases/v1.md": "232403661c48ea5966beaeeaf23f2797f446ee30742d4050b0e582be144a4bff",
    "runtime.md": "09a1fc319ced97c82d8fc16dd5c43cdb5afbc143f68e5c2d05f810e6382224b6",
    "security-and-reliability.md": "735fb37640bbbc3915ba7a709e79b6cfcfdb80847dbad8b4439d07c5cef3fb37",
    "skills.md": "86acd6b2b648f5fff11f26bd360c8b0a71a874a9a541003618d10379392e681a",
    "tasks-and-delegation.md": "9a0e83e914996a016e22e36f0fd34d720022b2eaac277c31d8bc8d795cc4dee9",
    "tools.md": "776249492516360d537382d50fb77f3dcbc6b063a6740dd090ddcc857f848f51",
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
