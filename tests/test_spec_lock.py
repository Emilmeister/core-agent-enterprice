import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "bc81f6ef9f3b6f2edbd0aefc246b2f99f33459a6c11811db6e7be556b9cd5df3",
    "a2a-protocol.md": "487beddb7435b46e515f598a95c5d5175fb218cf97e22e41b1762c97b8ada1c1",
    "acceptance.md": "20401e2ea6bcb2d37f228c64807c75e3c13a7bbe355c2ce8bbf3a5c7c7bb52d7",
    "agent-configuration.md": "ea86d8acb3f01a7bbcb1fe2d1cfb29196b619e950a87456c335ddc3d10112197",
    "architecture.md": "7fa84ab34ef9f10c577fa8db3a295c3eac4709dbb5762ef13aa11859971987fc",
    "artifacts.md": "34379a99378113f6735a1e76a64d96e4fb8d7de40f0d57b59ebcb0021f55345e",
    "context.md": "a102acafb230b2558bc210be41df85576c21d1cf1be46e519d0024152a40668f",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "704e06b07021c38f2d36c2d3eb6278de54d5d539abe079570100f72b665e781d",
    "implementation-status.md": "dc0ed0a5ffd820eed2b60267ae1cc0276e337302dbd03f8c348889f908761a15",
    "kernel-instructions.md": "b1af9f0283a5cfe3cc36d7eb23cdb3303edb30c3cc09b493cc402dfe0037dc23",
    "memory-service.md": "f27d9ec86866bf88de1a04924307c175f362983f39674e1c09aead057afb90d4",
    "observability.md": "ec995be053c59b2c18d94f28b451f30b51117598988ad3d888c0bcd79cc5d4db",
    "product.md": "1fcbf4b5cf5db21272da6e48f1bdb0ec906d306bdcc7ebecafeabf2bd1fcb6a9",
    "public-contract.md": "f7a3c6857848870bb6081d68c5573e9f416e47dde49a301bed752cfcc7e0cc1c",
    "releases/v1.md": "5c3f0d9a0cf91eca103fa562cdbbba69a267c75903a3a3bbc83185f8d9999555",
    "runtime.md": "724794632af96ed0e5e028e9c27569e05480245d31c82d8c8ec07933d5f2577e",
    "security-and-reliability.md": "3a18df4cb1a173d28b671a32245c15244d0858155f6ff6f29217f9abd80ef872",
    "skills.md": "86acd6b2b648f5fff11f26bd360c8b0a71a874a9a541003618d10379392e681a",
    "tasks-and-delegation.md": "122be1cd5619c07e8b6bdfcecba955b4fefea1185c79a7a27a23bcfba4c51bad",
    "tools.md": "796ad56427ceae94076c5d3fae6df459dee76d129068d000a613aee23fd91b0c",
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
