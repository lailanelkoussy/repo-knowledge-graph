class Item:
    """A product that can be put in a cart."""

    def __init__(self, name: str, price: float):
        self.name = name
        self.price = price


class DiscountedItem(Item):
    def __init__(self, name: str, price: float, discount: float):
        super().__init__(name, price)
        self.discount = discount

    def final_price(self) -> float:
        return self.price * (1 - self.discount)
