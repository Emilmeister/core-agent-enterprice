import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "a99d44c4b75da74031dede656981d44446e71e738eb05231bd185b2306723dc7",
    "acceptance.md": "25557134027062e62ae72521df50e6201354577d9de7520207d2db0a11d794a1",
    "agent-configuration.md": "3836ab01879594c5612fba52f5fcb9801dbd9106b1c4d6cd93a16b654cf06bc2",
    "architecture.md": "b562eb706d7be32099b1ee4ec5f16c2d1cc9085b371636e30c2509cc85b781fb",
    "artifacts.md": "72774798f6691e10bbaa8bdf4a40527ae1f447045156d7d32c0e091fe551305d",
    "context.md": "6d363c955ce5784b3b28114fdbb80e2d61d4c2885238d0e380c0bf04dcf127d1",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "1ad7324e418abbb4861c132422ce48bf995508c93dbb12ccc04ac30fd05bb681",
    "implementation-status.md": "03cd1923a621b4320ab4bfbe142ef2af4ad9f60f41d2c85edc488affe33a7049",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "cf3c35ab8908e75766ca87e241edfe7b7f87f6fb03c1c706f837f8ad130fde2c",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "c813883ef95b94eaf35a8b6b89285ce6b9a531d74905c0aa22e82720497c5281",
    "releases/v1.md": "4bcee2e09a5dca15a98f3e5ac6193a51fe83267c1e05aa5e5fd421d8e58f767b",
    "runtime.md": "15b4faa26d125c662522ef9c1deabf418651e697cf92e01a7d391b3a82e8fcdd",
    "security-and-reliability.md": "e9ef27ba75f534b14238544d515eb805ae04fb0a318bcccb9f8dd3616818fedb",
    "skills.md": "5a1a73c1ea3db1ddd4403534f49c04b2a83f1378b6199eb5be9c7b254b4202df",
    "tasks-and-delegation.md": "7a588a953ef89ecaf460c2813baf865bfcc71830aab16ccda8584098f3d56827",
    "tools.md": "9bf9b773c7c6a953f85ace76b8af8a0cb6352febb45a82f185183db6bca1469a",
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
