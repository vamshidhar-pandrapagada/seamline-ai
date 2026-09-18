from dataclasses import dataclass


@dataclass
class OrderCreated:
    order_id: str
    customer_id: str
    amount_cents: int
