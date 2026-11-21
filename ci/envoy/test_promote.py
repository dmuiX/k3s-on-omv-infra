import unittest
from pathlib import Path
from promote import ANCHOR, IMAGE, STOCK, render


class PromotionTests(unittest.TestCase):
    base = Path('01-bootstrap/istio-gateway/resources/gateway-config.yaml').read_text()
    digest = 'sha256:' + 'a' * 64
    upstream = '1.32.0-distroless'
    candidate = '1.32.0-envoy48409-123-1'

    def test_roundtrip(self):
        stock = render(self.base, 'stock', self.digest, self.upstream)
        patched = render(stock, 'patched', self.digest, self.candidate)
        self.assertIn(STOCK + '@' + self.digest + ' # ' + self.upstream, stock)
        self.assertIn(IMAGE + '@' + self.digest + ' # ' + self.candidate, patched)
        self.assertNotIn(self.upstream, patched)
        self.assertEqual(render(patched, 'patched', self.digest, self.candidate), patched)
        self.assertEqual(render(patched, 'stock', self.digest, self.upstream), stock)
        self.assertEqual(render(stock, 'stock', self.digest, self.upstream), stock)
        updated = render(stock, 'stock', 'sha256:' + 'b' * 64, '1.32.1-distroless')
        self.assertNotIn(self.digest, updated)
        self.assertNotIn(self.upstream, updated)

    def test_reject_unknown(self):
        unmanaged = self.base.replace(ANCHOR, ANCHOR+'              image: unmanaged\n')
        for text, mode, digest, tag in [(self.base, 'patched', 'latest', self.candidate), (self.base, 'other', self.digest, self.candidate), (self.base + ANCHOR, 'patched', self.digest, self.candidate), (unmanaged, 'stock', self.digest, self.upstream), (self.base, 'stock', self.digest, 'tag\ninvalid')]:
            with self.assertRaises(ValueError):
                render(text, mode, digest, tag)


if __name__ == '__main__':
    unittest.main()
