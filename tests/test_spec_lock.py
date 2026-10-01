import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "a99d44c4b75da74031dede656981d44446e71e738eb05231bd185b2306723dc7",
    "acceptance.md": "b82b71fac742eb837b3bb79f2cdc93ec82064e6212c90793f33767555dfda377",
    "agent-configuration.md": "3836ab01879594c5612fba52f5fcb9801dbd9106b1c4d6cd93a16b654cf06bc2",
    "architecture.md": "d0e47f885692b3627a251dac41860b05b3d98135a0b4ff8bd341765134e49bd2",
    "artifacts.md": "62515922d6d4ef36e09826c0518f23c52018ac8dd6fe68cd0f866af4001c1c13",
    "context.md": "6d363c955ce5784b3b28114fdbb80e2d61d4c2885238d0e380c0bf04dcf127d1",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "1ad7324e418abbb4861c132422ce48bf995508c93dbb12ccc04ac30fd05bb681",
    "implementation-status.md": "d8c66cefb9100152df2f72ccc3d4a246f3bcb7fef631a06aeb3bc2679b2fbd67",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "f3f0f2234139b827155e9271e31897f4884fc137dfa867a78420c64327b2b3cc",
    "releases/v1.md": "ba7156246dbfddcb90e27e35ec3fe6a67e23cdb128ddce8da68496ea44ae007d",
    "runtime.md": "15b4faa26d125c662522ef9c1deabf418651e697cf92e01a7d391b3a82e8fcdd",
    "security-and-reliability.md": "e9ef27ba75f534b14238544d515eb805ae04fb0a318bcccb9f8dd3616818fedb",
    "skills.md": "5a1a73c1ea3db1ddd4403534f49c04b2a83f1378b6199eb5be9c7b254b4202df",
    "tasks-and-delegation.md": "3f0d15f2304568a306b09615a52f0b4d391379446aa039c4ab9f87737cf3f257",
    "tools.md": "9851dc9454b02b0aa9b41639daa7942db649c0e52a8370b035208a4c7f84ddac",
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
