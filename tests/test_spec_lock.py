import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "3fbd47e36fe7f2ee3830afec0ec86f929fa06ee85d20581ef111402845449786",
    "a2a-protocol.md": "b7b8d4eb7167d98ae9e5713b18575e85708676506118a7845492ebcd2e5d565e",
    "acceptance.md": "a410b269510bc8f540fa4bd21ece2f4406f94c8bd3826561caeac0ea125be769",
    "agent-configuration.md": "4bfbff61adaa2100ba24b4321922545d40ad2b524fb3e5faec630ae7758685d4",
    "architecture.md": "e455a067f6be33932ae35149ec8be522e89bc939c84764c1c94ae0aac8c04f37",
    "artifacts.md": "93112c7f43a7823aa6bc4607d0215006a287a2fae120cdd3b2e4ca235fd2f8d3",
    "cloudru.md": "37cd87bffc27456cfadca97f13052a02fba67773d00b62c8bb8300bc78fe46cd",
    "context.md": "775883c92d5365a6476d2dab38e7428b8f5c1c1d98d67c0a7e6f20306f83d23d",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "4b2f780d7689a6e47d53ac141c557b8ecf1d144d2f73e4d8d8d472e4dfae9077",
    "implementation-status.md": "952eaccee38578f7fdb20415f68b63ddbdc68c6091b06dca058585dbeea1bc49",
    "kernel-instructions.md": "46b3c0b04bf23a23e88be443f75c721a3cee6aa220da38e6f330e12b59b1c932",
    "memory-service.md": "fb2381809c31bbf8679bb2fbeda7d6f69b1f7db37584a85b3d39af5ec0b23a27",
    "observability.md": "b70a7c9d436cea1cfc8321b5e68f21d86fbff1fee5a81661a5bf76308f35dfd0",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "c3b508e49b16d2137bad5b14a725b32b224931bfb60d015f1554209c9eead52a",
    "releases/v1.md": "57228d838bf9bcf3e98e1fd824eba5b7ff68577f632a3074fc0e25dce1410894",
    "runtime.md": "d470ff11149362029074080ae6478d1026eede51a7406230786a34ba928a9e2a",
    "security-and-reliability.md": "9673cd15c0add43c35f79d82fb9aba838170892f2c5149ded0df998893e3d4f5",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "070a89469719059215175319705d8822341fff782040c6b37b2816e2f459a680",
    "tools.md": "23a4eb397d2fbf512209f438a279dda5d67d6399eb89bbf215c9dfda75ba2231",
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
