import copy
import json
import unittest
from pathlib import Path
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
    def index_transaction(self):
        return json.loads((Path(__file__).parent / "fixtures" / "manifest_index_buy.json").read_text())

    def test_manifest_index_buy_uses_route_totals_not_wallet_leftovers(self):
        transaction = self.index_transaction()
        coin_type = "0xc466c28d87b3d5cd34f3d5c088751532d71a38d93a8aae4551dd56272cfb4355::manifest::MANIFEST"
        event = detect_buy(transaction, coin_type)

        self.assertEqual(event.amount, 9_162_159_175_791)
        self.assertEqual(event.sui_spent, 6_066_111_605)
        self.assertEqual(event.wallet_balance_change, 339_584_334_822)
        self.assertEqual(event.wallet, transaction["transaction"]["sender"])

    def test_counts_split_routes_once_and_excludes_other_tokens(self):
        transaction = self.index_transaction()
        # LOFI has split pool routes, but its confirmation is the final total.
        event = detect_buy(transaction, "0xf22da9a24ad027cccb5f2d496cbe91de953d363513db08a3a734d361c7c17503::LOFI::LOFI")
        self.assertEqual(event.amount, 721_345_219_855)
        self.assertEqual(event.sui_spent, 6_001_800_730)

    def test_aggregates_distinct_confirmed_purchases_of_same_token(self):
        transaction = self.index_transaction()
        route = copy.deepcopy(transaction["events"]["events"][0])
        route["parsed_json"]["quote_id"] = "second-purchase"
        transaction["events"]["events"].append(route)
        event = detect_buy(transaction, "0x" + route["parsed_json"]["target"])
        self.assertEqual(event.amount, 2 * int(route["parsed_json"]["amount_out"]))
        self.assertEqual(event.sui_spent, 2 * int(route["parsed_json"]["amount_in"]))

    def test_fully_deposited_swap_output_is_still_a_buy(self):
        transaction = self.index_transaction()
        route = transaction["events"]["events"][0]["parsed_json"]
        coin_type = "0x" + route["target"]
        transaction["balance_changes"] = [
            change for change in transaction["balance_changes"] if change["coin_type"] != coin_type
        ]
        event = detect_buy(transaction, coin_type)
        self.assertEqual(event.amount, int(route["amount_out"]))
        self.assertEqual(event.wallet_balance_change, 0)

    def test_cross_token_route_keeps_output_without_treating_input_as_sui(self):
        transaction = self.index_transaction()
        route = transaction["events"]["events"][0]["parsed_json"]
        route["from"] = "99::usdc::USDC"
        event = detect_buy(transaction, "0x" + route["target"])
        self.assertEqual(event.amount, int(route["amount_out"]))
        self.assertIsNone(event.sui_spent)

    def test_malformed_confirmations_never_fall_back_to_basket_spending(self):
        for field, value in (("amount_in", "bad"), ("amount_out", "0"),
                             ("amount_in", "18446744073709551616"),
                             ("fee_amount", "1"), ("quote_id", ""), ("target", None)):
            with self.subTest(field=field):
                transaction = self.index_transaction()
                route = transaction["events"]["events"][0]["parsed_json"]
                coin_type = "0x" + route["target"]
                route[field] = value
                self.assertIsNone(detect_buy(transaction, coin_type))

    def test_duplicate_quote_or_wrong_sender_is_not_attributed(self):
        for mutation in ("duplicate", "sender", "recipient", "sold"):
            with self.subTest(mutation=mutation):
                transaction = self.index_transaction()
                route = transaction["events"]["events"][0]
                coin_type = "0x" + route["parsed_json"]["target"]
                if mutation == "duplicate":
                    transaction["events"]["events"].append(copy.deepcopy(route))
                elif mutation == "sender":
                    route["sender"] = "0xother"
                elif mutation == "recipient":
                    transaction["balance_changes"].append({"address": "0xother", "coin_type": coin_type, "amount": "100"})
                else:
                    route["parsed_json"]["from"] = coin_type
                self.assertIsNone(detect_buy(transaction, coin_type))

    def test_unsupported_basket_and_untrusted_event_package_are_skipped(self):
        for mutation in ("missing", "spoofed"):
            transaction = self.index_transaction()
            coin_type = "0x" + transaction["events"]["events"][0]["parsed_json"]["target"]
            if mutation == "missing":
                transaction["events"] = None
            else:
                for event in transaction["events"]["events"]:
                    event["event_type"] = event["event_type"].replace("0xffd4058af7d6f6d66c335930cced8c91e38ec943e2392232aee27e8127e684e9", "0xfake")
            self.assertIsNone(detect_buy(transaction, coin_type))

    def test_index_shares_are_not_misclassified_as_a_swap_purchase(self):
        transaction = self.index_transaction()
        self.assertIsNone(detect_buy(transaction, "0xd45e443d4079bf6d1b5285be164f86ed58c1f873eb885306fb0f9e8bc5e5e662::crypto8::CRYPTO8"))

    def test_unsupported_deposit_after_swap_is_not_priced_from_leftovers(self):
        transaction = make_transaction()
        transaction.transaction.kind.programmable_transaction.commands.append(
            ns(move_call=ns(package="0xindex", module="index", function="deposit"))
        )
        self.assertIsNone(detect_buy(transaction, "0x2::demo::DEMO"))

    def test_unconfirmed_basket_or_multiple_inputs_is_not_attributed(self):
        for other_amount in ("500", "-500"):
            with self.subTest(other_amount=other_amount):
                transaction = make_transaction()
                transaction.balance_changes[-1].amount = "-1500"
                transaction.balance_changes.append(ns(
                    address="0xbuyer", coin_type="0x99::other::OTHER",
                    amount=other_amount,
                ))
                self.assertIsNone(detect_buy(transaction, "0x2::demo::DEMO"))

    def test_mixed_confirmed_inputs_do_not_report_partial_sui_cost(self):
        transaction = self.index_transaction()
        route = copy.deepcopy(transaction["events"]["events"][0])
        route["parsed_json"]["from"] = "99::usdc::USDC"
        route["parsed_json"]["quote_id"] = "cross-token-purchase"
        transaction["events"]["events"].append(route)
        event = detect_buy(transaction, "0x" + route["parsed_json"]["target"])
        self.assertEqual(event.amount, 2 * int(route["parsed_json"]["amount_out"]))
        self.assertIsNone(event.sui_spent)

    def test_nets_positive_and_negative_token_changes(self):
        transaction = make_transaction()
        transaction.balance_changes.append(ns(address="0xbuyer", coin_type="0x2::demo::DEMO", amount="-250"))
        self.assertEqual(detect_buy(transaction, "0x2::demo::DEMO").amount, 1000)

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
        self.assertEqual(event.sui_spent, 1000)

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
