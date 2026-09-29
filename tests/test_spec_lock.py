import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "333d8c714dd14322f1c52373aad8f414ae4f7174c0a3f0c32b7e8f5d5f12ba65",
    "a2a-protocol.md": "b2da3d3892393f0edb5252032c5e90b167ead09593b8f406b410cd98cea38950",
    "acceptance.md": "93d2001819489a54acbc38cba82102596023a6f955979434c554b587797ebc70",
    "agent-configuration.md": "fb152b9f67a02510fa7bbe1c9b5653e79ba1557c1e1e6694337a6c9ab06c6dc3",
    "architecture.md": "bab187a4c0c48bd5caa45c3d2c2dee92c5a96479b2b310e6ec45c94a52fbcf56",
    "artifacts.md": "b767a284e9d8e26d6554e6cdf9e8b744f10e89904041ba9324ad2d89541f38f0",
    "context.md": "30c91f79306873a683736cc9c6c056455d424f3f062eef0094fd017c78aef11e",
    "development-process.md": "3f0c3cd6881394705a921bbd767efddc56d5290c259b7ca9312eca13dc05a4c9",
    "execution-environment.md": "a25511e6cbbe5a9e2181f4cc95d9490ac6301ce10361c39672cde90747d8cdd1",
    "implementation-status.md": "b83e1d05b8da808b16d38ef8940a6bfc9f60d51a63ae34011a87d1dc97b46bdf",
    "kernel-instructions.md": "de0cf96432e9adfacf1d9a7bebbc3f0879373d274b6627f51d944915ce677ed6",
    "memory-service.md": "cf3c35ab8908e75766ca87e241edfe7b7f87f6fb03c1c706f837f8ad130fde2c",
    "observability.md": "14b7654cad48d5dbb3ea5cc407d0ecbd195abd7ea5e7213056a506a7d1fd7004",
    "product.md": "c13c57f37a78c746d18fed0b7f8c9c5a1025b8f1ca075aa5e7df414497d2b1c3",
    "public-contract.md": "a0655d9dbaf7151db444617948d061e15a4ed104a1a987401b7ef8cdd93beabd",
    "releases/v1.md": "4bcee2e09a5dca15a98f3e5ac6193a51fe83267c1e05aa5e5fd421d8e58f767b",
    "runtime.md": "eb1e61ad190b4ea7e80e41eae0912d077d2a56f605f36313459bd365a9b87ea8",
    "security-and-reliability.md": "6b772f58ddbbe3f64d4f73e7a2090b63969affbd62f094ba34c22ba6b18974d9",
    "skills.md": "5a1a73c1ea3db1ddd4403534f49c04b2a83f1378b6199eb5be9c7b254b4202df",
    "tasks-and-delegation.md": "74d52469258bcc6a7c38d60dd7c2bb4f3aa624ab79ccdf56f7c11d0fdc0183d4",
    "tools.md": "7752517cb7ef81613e1bfa3eb653a76e925349d56a9ac35b18c8d430ca4a1f48",
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
