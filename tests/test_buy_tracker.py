import unittest
from types import SimpleNamespace

from CityLedger.buy_tracker import canonicalize_sui_type, detect_buy


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def make_transaction(
    *,
    function="swap",
    module="router",
    package="0xabc",
    sender="0xbuyer",
    coin_type="0x2::demo::DEMO",
    amount=1250,
    recipient=None,
    success=True,
):
    move_call = ns(package=package, module=module, function=function)
    command = ns(move_call=move_call)
    programmable = ns(commands=[command])
    kind = ns(programmable_transaction=programmable)
    tx_data = ns(kind=kind, sender=sender)
    effects = ns(status=ns(success=success, error=None if success else "aborted"))
    balance_changes = [
        ns(address=recipient or sender, coin_type=coin_type, amount=str(amount)),
        ns(address=sender, coin_type="0x2::sui::SUI", amount="-500"),
    ]
    return ns(
        digest="Digest123",
        transaction=tx_data,
        effects=effects,
        events=None,
        checkpoint=99,
        timestamp="now",
        balance_changes=balance_changes,
    )


class BuyTrackerTests(unittest.TestCase):
    def test_detects_swap_and_reports_received_amount(self):
        event = detect_buy(make_transaction(), "0x2::demo::DEMO")

        self.assertIsNotNone(event)
        self.assertEqual(event.amount, 1250)
        self.assertEqual(event.wallet, "0xbuyer")
        self.assertEqual(event.digest, "Digest123")

    def test_rejects_positive_balance_change_without_swap_evidence(self):
        event = detect_buy(
            make_transaction(function="transfer", module="payments"),
            "0x2::demo::DEMO",
        )

        self.assertIsNone(event)

    def test_rejects_failed_transaction(self):
        event = detect_buy(make_transaction(success=False), "0x2::demo::DEMO")

        self.assertIsNone(event)

    def test_rejects_swap_when_selected_token_was_not_received(self):
        event = detect_buy(make_transaction(), "0x2::other::OTHER")

        self.assertIsNone(event)

    def test_uses_largest_recipient_when_sender_did_not_receive_token(self):
        transaction = make_transaction(sender="0xrouter", recipient="0xbuyer")
        transaction.balance_changes.append(
            ns(address="0xfee", coin_type="0x2::demo::DEMO", amount="5")
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertEqual(event.wallet, "0xbuyer")
        self.assertEqual(event.sender, "0xrouter")

    def test_aggregates_multiple_balance_changes_for_the_buyer(self):
        transaction = make_transaction(amount=1000)
        transaction.balance_changes.append(
            ns(address="0xbuyer", coin_type="0x2::demo::DEMO", amount="250")
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertEqual(event.amount, 1250)

    def test_infers_exchange_from_module_name(self):
        event = detect_buy(
            make_transaction(module="deepbook_router"),
            "0x2::demo::DEMO",
        )

        self.assertEqual(event.exchange, "DeepBook")

    def test_uses_configured_package_label(self):
        event = detect_buy(
            make_transaction(package="0xcafe"),
            "0x2::demo::DEMO",
            {"0x000cafe": "Example DEX"},
        )

        self.assertEqual(event.exchange, "Example DEX")

    def test_normalizes_padded_move_addresses(self):
        padded = "0x00000000000000000000000000000002::demo::DEMO"

        self.assertEqual(
            canonicalize_sui_type(padded),
            canonicalize_sui_type("0x2::demo::DEMO"),
        )
        event = detect_buy(
            make_transaction(coin_type=padded),
            "0x2::demo::DEMO",
        )
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
