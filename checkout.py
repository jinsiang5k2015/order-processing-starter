"""Checkout coordinator."""
from models import OrderResult


class CheckoutService:
    def __init__(self, repository, payment_gateway):
        self.repository = repository
        self.payment_gateway = payment_gateway

    def checkout(self, cart, payment_token, request_id):
        if not request_id:
            return OrderResult(None, 'missing_request_id', 0)

        existing = self.repository.get_order_by_request_id(request_id)
        if existing:
            return OrderResult(existing[0], existing[2], existing[1])

        if len(cart.lines) == 0:
            return OrderResult(None, 'empty', 0)

        amount = cart.total()
        if amount <= 0:
            return OrderResult(None, 'invalid_amount', 0)

        reserved = []
        for line in cart.lines:
            if not self.repository.update_stock(line.sku, line.quantity):
                for sku, qty in reserved:
                    self.repository.update_stock(sku, -qty)
                return OrderResult(None, 'out_of_stock', 0)
            reserved.append((line.sku, line.quantity))

        try:
            response = self.payment_gateway.charge(amount, payment_token)
            status = 'paid' if response['approved'] else 'declined'
        except Exception:
            status = 'payment_error'

        if status != 'paid':
            for sku, qty in reserved:
                self.repository.update_stock(sku, -qty)

        order_id = self.repository.save_order(cart.user_id, amount, status, request_id)

        if status == 'paid':
            cart.lines.clear()
            cart.discount_code = None
            cart.save()

        return OrderResult(order_id, status, amount)