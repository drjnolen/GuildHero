# Buybot transaction fixture

`manifest_index_buy.json` is a compact gRPC bridge projection of public Sui
transaction `7BJniP8cepr6M1nKRC89NPrDxn8TsxJcKP8V18xh2uBM`, checkpoint 332493404.
Repeated identical Move-call descriptors and unrelated pool events are omitted;
all balance changes and complete router confirmations are retained. The fixture
includes a MANIFEST pool-hop event to guard against double counting.

The basket spent 49.999999996 SUI across eight tokens. MANIFEST's route spent
6.066111605 SUI and produced 9,162.159175791 MANIFEST; only 339.584334822 remained
in the wallet after the index deposit. The old accounting divided the entire
basket spending by that remainder, inflating its derived price by about 222x.

USD price and supply in valuation tests are fixed test inputs, not captured
historical market-feed values.
