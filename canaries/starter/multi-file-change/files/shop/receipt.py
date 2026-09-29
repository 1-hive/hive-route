from .cart import Cart


def render(cart: Cart) -> str:
    lines = [f"{name} x{q}: {p * q:.2f}" for name, p, q in cart.items]
    lines.append(f"Total: {cart.total():.2f}")
    return "\n".join(lines)
