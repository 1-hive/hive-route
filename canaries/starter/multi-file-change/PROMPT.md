Add an optional discount to the shop in `shop/`:

1. `Cart.total()` takes an optional `discount_percent` (a number from 0 to 100, default 0)
   and returns the subtotal reduced by that percentage, rounded to 2 decimals by
   `pricing.round_money`. A value outside 0–100 raises `ValueError`.
2. `receipt.render(cart, discount_percent=0)` passes the discount on. When it is not 0,
   the receipt has a line `Discount (10%): -3.00` (the percentage as given, without a
   trailing `.0` for whole numbers, and the amount taken off) between the item lines and
   the `Total:` line.

Keep everything that works today working. Work only in this folder. When you're done,
reply with one line.
