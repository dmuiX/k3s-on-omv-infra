import unittest
from pathlib import Path
from promote import ANCHOR, IMAGE, render


class PromotionTests(unittest.TestCase):
    base = Path('01-bootstrap/istio-gateway/resources/gateway-config.yaml').read_text()
    digest = 'sha256:' + 'a' * 64

    def test_roundtrip(self):
        stock = render(self.base, 'stock', self.digest)
        patched = render(stock, 'patched', self.digest)
        self.assertIn(IMAGE + '@' + self.digest, patched)
        self.assertEqual(render(patched, 'patched', self.digest), patched)
        self.assertEqual(render(patched, 'stock', self.digest), stock)

    def test_reject_unknown(self):
        for text, mode, digest in [(self.base, 'patched', 'latest'), (self.base, 'other', self.digest), (self.base + ANCHOR, 'patched', self.digest), (self.base.replace(ANCHOR, ANCHOR+'              image: unmanaged\n'), 'stock', self.digest)]:
            with self.assertRaises(ValueError):
                render(text, mode, digest)


if __name__ == '__main__':
    unittest.main()
