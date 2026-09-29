"""Check for multi-file-change: the discount, and that nothing else broke."""
import os
import sys

sys.path.insert(0, os.environ["CANARY_WORK"])
from shop import receipt  # noqa: E402
from shop.cart import Cart  # noqa: E402

c = Cart()
c.add("tea", 2.5, 4)
c.add("cake", 10)
assert c.subtotal() == 20.0 and c.total() == 20.0
assert c.total(discount_percent=15) == 17.0
assert c.total(12.5) == 17.5
assert receipt.render(c) == "tea x4: 10.00\ncake x1: 10.00\nTotal: 20.00"
assert receipt.render(c, discount_percent=15) == (
    "tea x4: 10.00\ncake x1: 10.00\nDiscount (15%): -3.00\nTotal: 17.00")
assert receipt.render(c, discount_percent=12.5).splitlines()[2] == "Discount (12.5%): -2.50"
for bad in (-1, 100.5):
    try:
        c.total(discount_percent=bad)
    except ValueError:
        continue
    raise AssertionError(f"accepted {bad}")
d = Cart()
d.add("pen", 0.335, 3)
assert d.total(discount_percent=10) == 0.91
try:
    d.add("x", 1, 0)
except ValueError:
    pass
else:
    raise AssertionError("quantity 0 accepted")
print("ok")
