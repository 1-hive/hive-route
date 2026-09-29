from .pricing import round_money


class Cart:
    def __init__(self):
        self.items = []  # (name, unit_price, quantity)

    def add(self, name: str, unit_price: float, quantity: int = 1) -> None:
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        self.items.append((name, unit_price, quantity))

    def subtotal(self) -> float:
        return round_money(sum(p * q for _, p, q in self.items))

    def total(self) -> float:
        return self.subtotal()
