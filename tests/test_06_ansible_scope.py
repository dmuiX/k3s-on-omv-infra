import unittest
from contracts import ROOT


class AnsibleScopeTests(unittest.TestCase):
    def test_infra_does_not_own_ansible_or_host_inventory(self):
        forbidden = list(ROOT.glob("**/*.yml")) + list(ROOT.glob("**/*.yaml"))
        self.assertFalse(any("ansible" in path.parts or "inventory" in path.parts for path in forbidden))
        self.assertFalse((ROOT / "playbooks").exists())
        self.assertFalse((ROOT / "inventory").exists())


if __name__ == "__main__":
    unittest.main()
