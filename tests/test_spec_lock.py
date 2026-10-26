import hashlib
import unittest
from pathlib import Path


SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"

EXPECTED_SHA256 = {
    "README.md": "3fbd47e36fe7f2ee3830afec0ec86f929fa06ee85d20581ef111402845449786",
    "a2a-protocol.md": "5852a57d8b8120f4e0e4057ad50caea188fb43d60b45c3019ed9fc35c71d4a44",
    "acceptance.md": "0845a7f7dd142ed370e9ffd796d231cd42ed940edd692a7a9d56a1875641babc",
    "agent-configuration.md": "3e1da587f19e5c577261455214c8d81e4ca45dd6d7da36761125edd24296c5de",
    "architecture.md": "e455a067f6be33932ae35149ec8be522e89bc939c84764c1c94ae0aac8c04f37",
    "artifacts.md": "93112c7f43a7823aa6bc4607d0215006a287a2fae120cdd3b2e4ca235fd2f8d3",
    "cloudru.md": "37cd87bffc27456cfadca97f13052a02fba67773d00b62c8bb8300bc78fe46cd",
    "context.md": "775883c92d5365a6476d2dab38e7428b8f5c1c1d98d67c0a7e6f20306f83d23d",
    "development-process.md": "8898fb1b105bfefe4777e3b078ae777c067720a0790df194b5d39e357f9fcea9",
    "execution-environment.md": "4b2f780d7689a6e47d53ac141c557b8ecf1d144d2f73e4d8d8d472e4dfae9077",
    "implementation-status.md": "7b47a2306ef702a8c20b0b821a830e92c521f5177d903530dd2b8f019aea76cf",
    "kernel-instructions.md": "f8fd3c248305c7d0d397d2272b58bdf56bfbcaed99d7b0f6c4c6aaccc17ab0b3",
    "memory-service.md": "fb2381809c31bbf8679bb2fbeda7d6f69b1f7db37584a85b3d39af5ec0b23a27",
    "observability.md": "b70a7c9d436cea1cfc8321b5e68f21d86fbff1fee5a81661a5bf76308f35dfd0",
    "product.md": "b536ab4b1b84b3a71a103a0efb82ed8d31d78bd0d0c8c52505228757bc575a80",
    "public-contract.md": "2b5a0e27ef2838977c5047cd2ebb859e615ec711a69c6eefc75968c73f6e80ef",
    "releases/v1.md": "4a574ebfaeee97ff2cf40b9cbf67a4a9ac89096eda0386d2d857ea96e74757a3",
    "runtime.md": "f7242db032b2aab8ecb2f93b54248e0acbcb15ee8e4b6245357cfe1da9ca3e5c",
    "security-and-reliability.md": "9673cd15c0add43c35f79d82fb9aba838170892f2c5149ded0df998893e3d4f5",
    "skills.md": "fe9eb87031d97367c111f724748fa4d2ea3c05e44a3b7666034a28f4c6d2590a",
    "tasks-and-delegation.md": "070a89469719059215175319705d8822341fff782040c6b37b2816e2f459a680",
    "tools.md": "7fe6a1ca4dc44c2d4adb2bda43e3b5b30d264a58765d45a3ac67e7a7a3d72299",
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
