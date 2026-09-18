from decimal import Decimal


def handle_order_created(event: dict):
    """Process an order.created event and charge the customer."""
    order_id = event["order_id"]
    amount = Decimal(str(event["amount"]))
    # TODO: charge the customer via payment processor
    print(f"Charging ${amount} for order {order_id}")
