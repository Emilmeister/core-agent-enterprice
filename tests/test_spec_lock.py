import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "a99d44c4b75da74031dede656981d44446e71e738eb05231bd185b2306723dc7",
    "acceptance.md": "598d5c48da0333eaed41aad3efe66b0767b1d4dd2f52438599ee957267c55ae7",
    "agent-configuration.md": "ec2f63fa1dc11b3f785a65c7c99823c6bae399faed5b04966ea08fc55cd0feef",
    "architecture.md": "0e2d421b58dcbcc0a5cdf81e0ca24705e60363176d218e8d6d7a6aa9d76d4938",
    "artifacts.md": "8e9b9a8523542c515ef27b63eccfba3ef1ee56f43b7892e831878405e1ce69b2",
    "context.md": "6d363c955ce5784b3b28114fdbb80e2d61d4c2885238d0e380c0bf04dcf127d1",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "1ad7324e418abbb4861c132422ce48bf995508c93dbb12ccc04ac30fd05bb681",
    "implementation-status.md": "78dbed3a0cd6f64a39b195e99a7d5e9a65903535fc0f3f9d9407d84523e09832",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "454b5b741e90df5216ac1904e6fb17801bde876204bb0c369bf5e8923d775fd7",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "c8816783046d6eaa2899d5d3b39bf8fd7b9fea4917d667223fcb94a73097accf",
    "releases/v1.md": "d69697485bb3438a11335b8cac3656861d4ce906b5cddbd6f3b71a96c8754348",
    "runtime.md": "957ce7085a13464b8fb41c6e2ae114669c0e3c105558c99a98d0c52bcd82be6b",
    "security-and-reliability.md": "5a2dac2f793fefd317be9a35106d4c01096cfde15b3b191848f8373520e5ecb3",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "3f0d15f2304568a306b09615a52f0b4d391379446aa039c4ab9f87737cf3f257",
    "tools.md": "bf9cb01f4578e2c2d1e929a645b36c97b17bb4449c2d21638cf8af3fc5585788",
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
