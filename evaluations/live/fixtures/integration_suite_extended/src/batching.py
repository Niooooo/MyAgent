def batch_items(items, size):
    return [items[index : index + size] for index in range(0, len(items) + 1, size)]
