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
    effects = ns(
        status=ns(success=success, error=None if success else "aborted"),
        gas_used=ns(
            computation_cost="500",
            storage_cost="0",
            storage_rebate="0",
        ),
        gas_payer=sender,
    )
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

    def test_detects_unknown_venue_when_buyer_spent_another_token(self):
        transaction = make_transaction(function="execute", module="adapter")
        transaction.balance_changes[-1] = ns(
            address="0xbuyer",
            coin_type="0x99::usdc::USDC",
            amount="-1000000",
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertIsNotNone(event)
        self.assertEqual(event.amount, 1250)

    def test_detects_unknown_venue_when_sui_spend_exceeds_gas(self):
        transaction = make_transaction(function="execute", module="adapter")
        transaction.balance_changes[-1].amount = "-1500"

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertIsNotNone(event)

    def test_rejects_reward_claim_with_only_sui_gas_outflow(self):
        transaction = make_transaction(function="claim", module="rewards")

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertIsNone(event)

    def test_rejects_liquidity_withdrawal_with_lp_token_outflow(self):
        transaction = make_transaction(
            function="remove_liquidity",
            module="pool",
        )
        transaction.balance_changes[-1] = ns(
            address="0xbuyer",
            coin_type="0x99::pool::LP",
            amount="-1",
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertIsNone(event)

    def test_infers_turbos_from_known_wrapper_package(self):
        transaction = make_transaction(
            function="execute",
            module="adapter",
            package="0x8b14f4351bb342b81c27fce2fe6d0f56b98288dc88fbe60b28b26d804b25941a",
        )
        transaction.balance_changes[-1] = ns(
            address="0xbuyer",
            coin_type="0x99::usdc::USDC",
            amount="-1000000",
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertEqual(event.exchange, "Turbos")

    def test_infers_cetus_from_wrapped_swap_event_type(self):
        transaction = make_transaction(module="router")
        transaction.events = ns(
            events=[
                ns(
                    package_id="0xwrapper",
                    module="router",
                    event_type=(
                        "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592"
                        "abaf7a35689b2fb::pool::SwapEvent"
                    ),
                )
            ]
        )

        event = detect_buy(transaction, "0x2::demo::DEMO")

        self.assertEqual(event.exchange, "Cetus")

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
