from .models import Item


class Cart:
    def __init__(self):
        self.items = []

    def add_item(self, item: Item):
        self.items.append(item)

    def total(self) -> float:
        return compute_total(self.items)


def compute_total(items) -> float:
    return sum(item.price for item in items)
