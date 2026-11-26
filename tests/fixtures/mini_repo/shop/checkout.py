from shop.cart import Cart, compute_total
from shop.models import Item


def checkout(names_and_prices):
    cart = Cart()
    for name, price in names_and_prices:
        cart.add_item(Item(name, price))
    return compute_total(cart.items)
