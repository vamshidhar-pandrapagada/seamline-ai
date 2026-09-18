# payments

Consumes `order.created` to charge the customer. In the demo, this service gets built
(wrongly) to read `amount` instead of `amount_cents`: the planted mismatch Seamline should catch.
