# Intentional Defects: Identified and Fixed

Order-processing system, 12 defects across 3 files (`repository.py`, `checkout.py`, `cart.py`).

## Summary

| # | Defect | File | Category |
|---|--------|------|----------|
| 1 | SQL injection in `get_saved_cart` | `repository.py` | Security |
| 2 | SQL injection in `get_product` | `repository.py` | Security |
| 3 | `list_orders` ignores `user_id` | `repository.py` | Security |
| 4 | Payment token printed to console | `checkout.py` | Security |
| 5 | Stock decremented before payment | `checkout.py` | Correctness |
| 6 | Cart cleared even when payment fails | `checkout.py` | Correctness |
| 7 | Stock can go negative | `repository.py` | Correctness |
| 8 | Zero or negative amount sent to payment gateway | `checkout.py` | Correctness |
| 9 | Negative or zero quantity accepted | `cart.py` | Validation |
| 10 | Stock check ignores cart contents | `cart.py` | Validation |
| 11 | `SAVE15` can make the total negative | `cart.py` | Correctness |
| 12 | Discount compounding | `cart.py` | Correctness |

---

## repository.py

### 1. SQL injection in `get_saved_cart`

**Problem:** `user_id` is pasted into the SQL with an f-string, so crafted input changes the query.

**Before:**

```python
rows = conn.execute(f"SELECT sku, quantity FROM saved_cart WHERE user_id = '{user_id}'").fetchall()
```

**After (parameterized query):**

```python
rows = conn.execute(
    "SELECT sku, quantity FROM saved_cart WHERE user_id = ?",
    (user_id,)
).fetchall()
```

### 2. SQL injection in `get_product`

**Problem:** `sku` is pasted into the SQL with an f-string. A value like `' OR '1'='1` matches every product.

**Before:**

```python
row = conn.execute(f"SELECT sku, name, price, stock FROM products WHERE sku = '{sku}'").fetchone()
```

**After:**

```python
row = conn.execute(
    "SELECT sku, name, price, stock FROM products WHERE sku = ?",
    (sku,)
).fetchone()
```

### 3. `list_orders` ignores `user_id`

**Problem:** The method takes `user_id` but never uses it, so every caller receives all users' orders (data leak).

**Before:**

```python
rows = conn.execute('SELECT id, user_id, amount, status FROM orders ORDER BY id DESC').fetchall()
```

**After:**

```python
rows = conn.execute(
    'SELECT id, user_id, amount, status FROM orders WHERE user_id = ? ORDER BY id DESC',
    (user_id,)
).fetchall()
```

### 7. Stock can go negative

**Problem:** `update_stock` subtracts without checking the remaining stock, so it can drop below zero (overselling).

**Before:**

```python
def update_stock(self, sku, quantity):
    conn = self.connect()
    conn.execute('UPDATE products SET stock = stock - ? WHERE sku = ?', (quantity, sku))
    conn.commit()
    conn.close()
```

**After:** a `stock >= ?` guard, a returned success flag, and `try/finally` so the connection always closes.

```python
def update_stock(self, sku, quantity):
    conn = self.connect()
    try:
        cur = conn.execute(
            'UPDATE products SET stock = stock - ? WHERE sku = ? AND stock >= ?',
            (quantity, sku, quantity))
        conn.commit()
        return cur.rowcount == 1   # False = not enough stock
    finally:
        conn.close()
```

---

## checkout.py

### 4. Payment token printed to console

**Problem:** A sensitive value is written to stdout and logs.

**Before:**

```python
print(f'Charging payment token: {payment_token}')
```

**After:** the line is removed.

### 5 and 6. Stock decremented before payment, and cart cleared when payment fails

**Problem 5:** Stock is reduced before the charge, so a declined or failed payment still loses stock.

**Problem 6:** The cart is cleared and saved even when payment fails, so a declined customer loses their cart.

Both are fixed by one restructure of `checkout()`: charge first, then change stock and clear the cart only if the status is `'paid'`. The "Large order" block was also removed because it only printed messages and had no effect.

**Before:**

```python
def checkout(self, cart, payment_token, request_id):
    if len(cart.lines) == 0:
        return OrderResult(None, 'empty', 0)
    amount = cart.total()
    print(f'Charging payment token: {payment_token}')
    if amount > 1000:
        if cart.user_id:
            if not request_id:
                print('Missing request id')
            else:
                print('Large order')
    for line in cart.lines:
        self.repository.update_stock(line.sku, line.quantity)
    try:
        response = self.payment_gateway.charge(amount, payment_token)
        status = 'paid' if response['approved'] else 'declined'
    except Exception:
        status = 'payment_error'
    order_id = self.repository.save_order(cart.user_id, amount, status, request_id)
    cart.lines.clear()
    cart.discounted_total = None
    cart.save()
    return OrderResult(order_id, status, amount)
```

**After:**

```python
def checkout(self, cart, payment_token, request_id):
    if len(cart.lines) == 0:
        return OrderResult(None, 'empty', 0)
    amount = cart.total()
    if amount <= 0:                                   # defect 8
        return OrderResult(None, 'invalid_amount', 0)

    # Charge first
    try:
        response = self.payment_gateway.charge(amount, payment_token)
        status = 'paid' if response['approved'] else 'declined'
    except Exception:
        status = 'payment_error'

    # Change stock only after a successful payment (defect 5)
    if status == 'paid':
        for line in cart.lines:
            self.repository.update_stock(line.sku, line.quantity)

    order_id = self.repository.save_order(cart.user_id, amount, status, request_id)

    # Clear the cart only after a successful payment (defect 6)
    if status == 'paid':
        cart.lines.clear()
        cart.discounted_total = None
        cart.save()

    return OrderResult(order_id, status, amount)
```

### 8. Zero or negative amount sent to the payment gateway

**Problem:** After `amount = cart.total()` nothing checks the amount, so a total of 0 or less is "charged" and the fake gateway approves it.

**Before:**

```python
amount = cart.total()
```

**After:**

```python
amount = cart.total()
if amount <= 0:
    return OrderResult(None, 'invalid_amount', 0)
```

---

## cart.py

### 9. Negative or zero quantity accepted

**Problem:** `add_item('MOU-01', -5)` passes the stock check (`stock < -5` is false) and reduces the cart.

**Fix:** add at the top of `add_item`:

```python
if quantity <= 0:
    print('Invalid quantity')
    return False
```

### 10. Stock check ignores cart contents

**Problem:** With 5 in stock, adding 3 twice passes both checks and the cart holds 6.

**Fix:** move `existing` above the check and include what is already in the cart:

```python
existing = next((x for x in self.lines if x.sku == sku), None)
in_cart = existing.quantity if existing else 0
if product.stock < in_cart + quantity:
    print('Not enough stock')
    return False
```

### 11. `SAVE15` can make the total negative

**Problem:** A $9.99 cart becomes -$5.01.

**Before:**

```python
elif code == 'SAVE15':
    amount -= 15
```

**After:**

```python
elif code == 'SAVE15':
    amount = max(amount - 15, 0)
```

### 12. Discount compounding

**Problem:** `apply_discount` starts from `self.total()`, which is already discounted if a code was applied before. Applying `WELCOME10` twice gives 20% off.

**Before:**

```python
amount = self.total()
```

**After:**

```python
amount = self.subtotal()
```

---

## Notes

- Defects 1 and 2 are the same bug type in two different places and are counted separately, as confirmed by the instructor.
- Defects 5 and 6 share one code change in `checkout()` but are two distinct problems.
- Defect 8 overlaps with defect 11, since a zero total can still occur after the `SAVE15` fix.