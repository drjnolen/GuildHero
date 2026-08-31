# Subscription operations

## Deployment and data

Set `PREMIUM_WHITELIST_CHAT_IDS`, `SUBSCRIPTION_PRICE_STARS` (default 250), and
`PAYMENT_SUPPORT_URL` in the bot host's environment. Set the whitelist **before**
deploying if existing groups should retain complimentary access. A blank whitelist
intentionally turns chat analytics off in every group without a paid subscription.
The price is a fixed number of Stars, not a currency conversion. Payment support
must be a real operator contact, not Telegram support.

The migration only creates `subscription_orders`, `group_subscriptions`, and
`subscription_payments`, plus a payment index. It does not delete chat history,
stats, wallet data, events, or feature settings. Payment data must be included in
backups. New ordinary messages are ignored before text inspection, migration, or
history/stat writes when access is inactive. Private ordinary messages are also
ignored; explicit wallet and configuration conversations continue to work.

All AI commands, chat stats/badges, leaderboard exports, and leaderboard-based
airdrop/raffle operations check the source group's access. Expiration is rechecked
between users during long scoring jobs. Work already submitted to the AI provider
cannot be recalled when a subscription expires. No new quotas have been imposed
on existing paid/whitelisted features; existing sampling, input limits, caching,
and command rate limits still apply. Monitor real per-group costs before promising
unlimited usage at this price.

## Billing behavior

- A current group admin accepts displayed terms, then gets an invoice link.
  The saved order binds the group, purchaser, price, and terms version. A changed
  price or terms version requires accepting the new terms before a new invoice.
- Orders expire for initial checkout after one hour. The pre-checkout handler
  checks the saved payer/price, current admin status, whitelist, existing paid
  access, and a transactional two-minute checkout reservation. No entitlement
  is granted at this stage.
- `SuccessfulPayment` activates or renews access using Telegram's authoritative
  `subscription_expiration_date`. The charge ID is unique in the database;
  repeated or out-of-order delivery cannot add an extra month or shorten access.
  Renewal uses the saved price even if the environment's price changes or the
  payer is no longer an admin. Subscription ownership does not silently transfer.
- A delayed second invoice that overlaps an active subscription is marked for
  refund, refunded via Telegram, and canceled rather than granting duplicate
  access. Checkout/payment transactions lock the group row, independently of the
  bot's ordinary message-storage connection.
- The payer can cancel through `/subscription` or Telegram's subscription settings.
  Bot cancellation stops renewal without changing the paid expiration. Telegram
  does not send this bot a cancellation notification for every external change;
  the UI says “manage renewal” until a bot-initiated cancellation is recorded.
- Refund service messages mark the charge refunded and recalculate access from
  the remaining non-refunded periods. Refunded payments cannot reactivate access
  if their success event is delivered again. Refunds do not inherently cancel
  future renewal; when resolving a refund, cancel the subscription too if agreed
  with the payer.
- Billing updates can run while a slow score or other normal handler is waiting.
  Other updates remain serialized, preserving calendar/wallet conversation and
  money-transfer behavior. The update processor bounds concurrency to 256.

## Support and reconciliation

`/paysupport` directs users to your configured private support contact. The bot
does not promise that Telegram support will resolve purchases on your behalf.
Use Telegram's [refundStarPayment](https://core.telegram.org/bots/api#refundstarpayment)
and [editUserStarSubscription](https://core.telegram.org/bots/api#edituserstarsubscription)
with the saved charge ID and payer ID when resolving an operator-approved refund.
Never post tokens, full receipts, or payer information in public issue trackers.

If payment persistence fails, the handler retries three times and asks the user
to contact support without paying again. Alert on subscription persistence,
unmatched payment/refund, or duplicate-refund errors. Telegram polling is not a
durable billing event queue: an extended database outage or process crash can
require manual reconciliation against
[getStarTransactions](https://core.telegram.org/bots/api#getstartransactions) and
the purchaser's receipt. Do not fabricate a payment or manually extend expiration
without verifying it against Telegram. An operator must resolve any automatic
refund/cancellation API failure before considering the incident closed.

Run a single polling replica. External refunds/configuration changes and any
other process's entitlement changes can take up to 60 seconds to be observed by
the access cache. Whitelist changes take effect after restart. If Telegram changes
a group's ID on conversion to a supergroup, update the whitelist; paid group-ID
changes currently require operator migration of the billing records. Do not ask
the group to pay twice. Existing bot settings also use numeric group IDs.

## Verification before taking real payments

The automated tests never charge Stars or contact Telegram. Use Telegram's
[dedicated test environment](https://core.telegram.org/bots/payments-stars#testing-payments)
with a separate test bot/database for this acceptance checklist:

1. An unlisted group's ordinary messages create no message/user-stat records.
   Every gated command shows the inline Subscribe button; free tools still work.
2. A whitelisted group retains summaries, scoring, stats, badges, exports, and
   leaderboard rewards without an invoice.
3. A group admin opens `/subscribe`, reads/accepts terms, and sees a 250-Star,
   30-day recurring invoice. Another user cannot pay that saved order.
4. Declined or abandoned checkout leaves tracking off. A successful payment
   enables only the purchased group, even when its receipt arrives in a DM.
5. Exercise duplicate delivery, renewal, cancellation, expiry, restart, refund,
   and a blocked old score/CSV button. Verify the corresponding database records.
6. Confirm the support contact, terms, backup/restore, and refund procedures with
   the operator before enabling live purchases. No production deployment or live
   payment test is performed by opening this PR.

Run offline tests in separate processes because the legacy suite replaces SDK
modules with mocks:

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s billing_tests -v
npm run check:sui
npm run test:sui
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to run the billing
transaction tests too. They create and remove a uniquely named test schema.
The GitHub Actions workflow runs these against its own PostgreSQL 16 service,
including concurrent checkout/payment tests. Never point test configuration at
production.
