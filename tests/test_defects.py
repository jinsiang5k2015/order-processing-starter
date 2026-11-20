import pytest

from cart import ShoppingCart
from checkout import CheckoutService
from payments import FakePaymentGateway
from repository import StoreRepository
from setup_db import initialize


@pytest.fixture
def repo(tmp_path):
    db = str(tmp_path / 'test.db')
    initialize(db)
    return StoreRepository(db)


@pytest.fixture
def cart(repo):
    return ShoppingCart('u1', repo)


class ExplodingGateway:
    def __init__(self):
        self.calls = []

    def charge(self, amount, payment_token):
        self.calls.append((amount, payment_token))
        raise RuntimeError('gateway down')


def test_get_product_returns_existing_product(repo):
    product = repo.get_product('MOU-01')
    assert product.name == 'Wireless mouse'
    assert product.stock == 20


def test_get_product_unknown_sku_returns_none(repo):
    assert repo.get_product('NOPE') is None


def test_get_product_is_not_sql_injectable(repo):
    assert repo.get_product("' OR '1'='1") is None


def test_get_saved_cart_is_not_sql_injectable(repo, cart):
    cart.add_item('MOU-01', 2)
    cart.save()
    assert repo.get_saved_cart("' OR '1'='1") == []


def test_list_orders_returns_only_the_users_orders(repo):
    repo.save_order('alice', 10.0, 'paid', 'r1')
    repo.save_order('bob', 20.0, 'paid', 'r2')
    repo.save_order('alice', 30.0, 'declined', 'r3')

    orders = repo.list_orders('alice')

    assert len(orders) == 2
    assert all(row[1] == 'alice' for row in orders)


def test_update_stock_reduces_stock(repo):
    assert repo.update_stock('MOU-01', 3) is True
    assert repo.get_product('MOU-01').stock == 17


def test_update_stock_cannot_go_negative(repo):
    assert repo.update_stock('LAP-01', 10) is False
    assert repo.get_product('LAP-01').stock == 5


@pytest.mark.parametrize('qty', [0, -1, -5])
def test_add_item_rejects_zero_and_negative_quantity(cart, qty):
    assert cart.add_item('MOU-01', qty) is False
    assert cart.lines == []


def test_add_item_unknown_product_is_rejected(cart):
    assert cart.add_item('NOPE', 1) is False
    assert cart.lines == []


def test_add_duplicate_product_merges_into_one_line(cart):
    cart.add_item('MOU-01', 2)
    cart.add_item('MOU-01', 3)
    assert len(cart.lines) == 1
    assert cart.lines[0].quantity == 5


def test_stock_check_includes_quantity_already_in_cart(cart):
    assert cart.add_item('LAP-01', 3) is True
    assert cart.add_item('LAP-01', 3) is False
    assert cart.lines[0].quantity == 3
    assert cart.add_item('LAP-01', 2) is True
    assert cart.lines[0].quantity == 5


def test_remove_item_partial_then_full(cart):
    cart.add_item('MOU-01', 3)
    assert cart.remove_item('MOU-01', 1) is True
    assert cart.lines[0].quantity == 2
    assert cart.remove_item('MOU-01') is True
    assert cart.lines == []


def test_remove_item_not_in_cart_returns_false(cart):
    assert cart.remove_item('MOU-01') is False


def test_subtotal_sums_all_lines(cart):
    cart.add_item('MOU-01', 2)
    cart.add_item('USB-01', 1)
    assert cart.subtotal() == pytest.approx(69.89)


@pytest.mark.parametrize('code, factor', [('WELCOME10', 0.9), ('STUDENT20', 0.8)])
def test_percentage_discounts(cart, code, factor):
    cart.add_item('MOU-01', 2)
    assert cart.apply_discount(code) is True
    assert cart.total() == pytest.approx(59.90 * factor)


def test_save15_never_makes_total_negative(cart):
    cart.add_item('USB-01', 1)
    cart.apply_discount('SAVE15')
    assert cart.total() == 0


def test_save15_subtracts_15_on_larger_cart(cart):
    cart.add_item('MOU-01', 2)
    cart.apply_discount('SAVE15')
    assert cart.total() == pytest.approx(44.90)


def test_same_discount_applied_twice_does_not_compound(cart):
    cart.add_item('MOU-01', 2)
    cart.apply_discount('WELCOME10')
    cart.apply_discount('WELCOME10')
    assert cart.total() == pytest.approx(59.90 * 0.9)


def test_invalid_coupon_is_rejected_and_total_unchanged(cart):
    cart.add_item('MOU-01', 2)
    assert cart.apply_discount('BOGUS') is False
    assert cart.total() == pytest.approx(59.90)


def test_saved_cart_can_be_loaded_into_a_new_cart(repo, cart):
    cart.add_item('MOU-01', 2)
    cart.add_item('USB-01', 1)
    cart.save()

    restored = ShoppingCart('u1', repo)
    restored.load()

    assert {(l.sku, l.quantity) for l in restored.lines} == {('MOU-01', 2), ('USB-01', 1)}


def test_saving_again_replaces_the_previous_saved_cart(repo, cart):
    cart.add_item('MOU-01', 2)
    cart.save()
    cart.remove_item('MOU-01')
    cart.add_item('USB-01', 4)
    cart.save()

    assert repo.get_saved_cart('u1') == [('USB-01', 4)]


def test_checkout_empty_cart_does_not_charge(repo, cart):
    gateway = FakePaymentGateway()
    result = CheckoutService(repo, gateway).checkout(cart, 'TEST_TOKEN', 'req-1')
    assert result.status == 'empty'
    assert gateway.calls == []


def test_checkout_paid_reduces_stock_saves_order_and_clears_cart(repo, cart):
    cart.add_item('MOU-01', 2)
    result = CheckoutService(repo, FakePaymentGateway()).checkout(cart, 'TEST_TOKEN', 'req-1')

    assert result.status == 'paid'
    assert result.total == pytest.approx(59.90)
    assert repo.get_product('MOU-01').stock == 18
    assert cart.lines == []
    assert repo.get_saved_cart('u1') == []
    assert [o[3] for o in repo.list_orders('u1')] == ['paid']


def test_checkout_declined_keeps_stock_and_cart(repo, cart):
    cart.add_item('MOU-01', 2)
    service = CheckoutService(repo, FakePaymentGateway(should_approve=False))

    result = service.checkout(cart, 'TEST_TOKEN', 'req-1')

    assert result.status == 'declined'
    assert repo.get_product('MOU-01').stock == 20
    assert len(cart.lines) == 1
    assert cart.lines[0].quantity == 2


def test_checkout_unexpected_gateway_error_is_handled(repo, cart):
    cart.add_item('MOU-01', 2)
    gateway = ExplodingGateway()

    result = CheckoutService(repo, gateway).checkout(cart, 'TEST_TOKEN', 'req-1')

    assert result.status == 'payment_error'
    assert repo.get_product('MOU-01').stock == 20
    assert len(cart.lines) == 1


def test_checkout_zero_total_is_not_sent_to_gateway(repo, cart):
    gateway = FakePaymentGateway()
    cart.add_item('USB-01', 1)
    cart.apply_discount('SAVE15')

    result = CheckoutService(repo, gateway).checkout(cart, 'TEST_TOKEN', 'req-1')

    assert result.status == 'invalid_amount'
    assert gateway.calls == []
    assert repo.get_product('USB-01').stock == 50


def test_checkout_does_not_print_the_payment_token(repo, cart, capsys):
    cart.add_item('MOU-01', 1)
    CheckoutService(repo, FakePaymentGateway()).checkout(cart, 'SECRET_TOKEN_123', 'req-1')
    assert 'SECRET_TOKEN_123' not in capsys.readouterr().out