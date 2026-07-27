import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildTransferTransaction,
  handleRequest,
  keypairFromPrivateKeyHex,
  mapCheckpoint,
  sanitizeError,
} from '../CityLedger/sui_bridge.mjs';

test('maps only buy-detection fields from a gRPC checkpoint', () => {
  const mapped = mapCheckpoint({
    sequenceNumber: 101n,
    transactions: [
      {
        digest: 'digest-1',
        transaction: {
          sender: '0xbuyer',
          kind: {
            data: {
              oneofKind: 'programmableTransaction',
              programmableTransaction: {
                inputs: [{ intentionally: 'discarded' }],
                commands: [
                  {
                    command: {
                      oneofKind: 'moveCall',
                      moveCall: {
                        package: '0xcetus',
                        module: 'pool',
                        function: 'swap_a2b',
                        arguments: [{ intentionally: 'discarded' }],
                      },
                    },
                  },
                  {
                    command: {
                      oneofKind: 'transferObjects',
                      transferObjects: {},
                    },
                  },
                ],
              },
            },
          },
        },
        effects: {
          status: {
            success: true,
            error: {
              nestedProtocolField: 123n,
            },
          },
          gasUsed: {
            computationCost: 100n,
            storageCost: 50n,
            storageRebate: 20n,
          },
          gasObject: {
            inputOwner: {
              address: '0xbuyer',
            },
          },
        },
        events: {
          events: [
            {
              packageId: '0xcetus',
              module: 'pool',
              eventType: '0xcetus::pool::SwapEvent',
              sender: '0xbuyer',
              contents: new Uint8Array([1, 2, 3]),
            },
          ],
        },
        checkpoint: 101n,
        timestamp: { seconds: 123n, nanos: 456 },
        balanceChanges: [
          {
            address: '0xbuyer',
            coinType: '0xabc::coin::COIN',
            amount: '2500000',
          },
        ],
      },
    ],
  });

  assert.deepEqual(mapped, {
    sequenceNumber: '101',
    transactions: [
      {
        digest: 'digest-1',
        transaction: {
          sender: '0xbuyer',
          kind: {
            programmable_transaction: {
              commands: [
                {
                  move_call: {
                    package: '0xcetus',
                    module: 'pool',
                    function: 'swap_a2b',
                  },
                },
              ],
            },
          },
        },
        effects: {
          status: { success: true, error: 'Transaction failed' },
          gas_used: {
            computation_cost: '100',
            storage_cost: '50',
            storage_rebate: '20',
          },
          gas_payer: '0xbuyer',
        },
        events: {
          events: [
            {
              package_id: '0xcetus',
              module: 'pool',
              event_type: '0xcetus::pool::SwapEvent',
              sender: '0xbuyer',
            },
          ],
        },
        checkpoint: 101,
        timestamp: { seconds: '123', nanos: 456 },
        balance_changes: [
          {
            address: '0xbuyer',
            coin_type: '0xabc::coin::COIN',
            amount: '2500000',
          },
        ],
      },
    ],
  });
});

test('latest checkpoint request converts bigint to a JSON-safe string', async () => {
  const client = {
    ledgerService: {
      getServiceInfo: async () => ({
        response: { checkpointHeight: 999n },
      }),
    },
  };

  const result = await handleRequest(
    { method: 'latestCheckpoint', params: {} },
    client,
  );

  assert.deepEqual(result, { sequenceNumber: '999' });
});

test('fetches checkpoint batches with bounded concurrency and preserves sequence order', async () => {
  let inFlight = 0;
  let peakInFlight = 0;
  const client = {
    ledgerService: {
      getCheckpoint: async ({ checkpointId }) => {
        inFlight += 1;
        peakInFlight = Math.max(peakInFlight, inFlight);
        const sequenceNumber = checkpointId.sequenceNumber;
        await new Promise((resolve) =>
          setTimeout(resolve, Number(13n - sequenceNumber)),
        );
        inFlight -= 1;
        return {
          response: {
            checkpoint: {
              sequenceNumber,
              transactions: [],
            },
          },
        };
      },
    },
  };

  const result = await handleRequest(
    {
      method: 'checkpoints',
      params: {
        sequenceNumbers: Array.from({ length: 12 }, (_, index) =>
          String(index + 1),
        ),
      },
    },
    client,
  );

  assert.deepEqual(
    result.checkpoints.map((checkpoint) => checkpoint.sequenceNumber),
    Array.from({ length: 12 }, (_, index) => String(index + 1)),
  );
  assert.equal(peakInFlight, 5);
});

test('retries timed-out checkpoint calls with an RPC deadline', async () => {
  let attempts = 0;
  const client = {
    ledgerService: {
      getCheckpoint: async ({ checkpointId }, options) => {
        attempts += 1;
        assert.equal(options.timeout, 10_000);
        if (attempts === 1) {
          throw new Error('temporary upstream timeout');
        }
        return {
          response: {
            checkpoint: {
              sequenceNumber: checkpointId.sequenceNumber,
              transactions: [],
            },
          },
        };
      },
    },
  };

  const result = await handleRequest(
    {
      method: 'checkpoints',
      params: { sequenceNumbers: ['42'] },
    },
    client,
  );

  assert.equal(attempts, 2);
  assert.equal(result.checkpoints[0].sequenceNumber, '42');
});

test('drains finalized checkpoints from the live subscription', async () => {
  const subscribedRequests = [];
  const client = {
    subscriptionService: {
      subscribeCheckpoints: (request) => {
        subscribedRequests.push(request);
        return {
          responses: {
            async *[Symbol.asyncIterator]() {
              for (const sequenceNumber of [201n, 202n, 203n]) {
                yield {
                  cursor: sequenceNumber,
                  checkpoint: {
                    sequenceNumber,
                    transactions: [],
                  },
                };
              }
            },
          },
        };
      },
    },
  };

  const result = await handleRequest(
    {
      method: 'subscribedCheckpoints',
      params: { maxItems: 3, waitMs: 100 },
    },
    client,
  );

  assert.deepEqual(
    result.checkpoints.map((checkpoint) => checkpoint.sequenceNumber),
    ['201', '202', '203'],
  );
  assert.equal(subscribedRequests.length, 1);
  assert.ok(
    subscribedRequests[0].readMask.paths.includes(
      'transactions.balance_changes',
    ),
  );
});

test('rejects oversized checkpoint batches', async () => {
  await assert.rejects(
    handleRequest(
      {
        method: 'checkpoints',
        params: {
          sequenceNumbers: Array.from({ length: 51 }, (_, index) => index),
        },
      },
      {},
    ),
    /more than 50 checkpoints/,
  );
});

test('builds a Sui v2 coin intent and transfer PTB', () => {
  const transaction = buildTransferTransaction({
    recipient: '0x1',
    amount: '2500000',
    coinType: '0xabc::coin::COIN',
    gasBudget: '50000000',
  });
  const data = transaction.getData();

  assert.equal(data.gasData.budget, '50000000');
  assert.equal(data.commands[0].$kind, '$Intent');
  assert.deepEqual(data.commands[0].$Intent.data, {
    type: `0x${'0'.repeat(61)}abc::coin::COIN`,
    balance: 2500000n,
    outputKind: 'coin',
  });
  assert.equal(data.commands[1].$kind, 'TransferObjects');
});

test('rejects zero-value transfers before signing', () => {
  assert.throws(
    () =>
      buildTransferTransaction({
        recipient: '0x1',
        amount: '0',
        coinType: '0x2::sui::SUI',
        gasBudget: '50000000',
      }),
    /greater than zero/,
  );
});

test('official SDK derives the same address as the Python wallet setup', () => {
  const keypair = keypairFromPrivateKeyHex('1'.repeat(64));

  assert.equal(
    keypair.toSuiAddress(),
    '0x0881c07520943bbf13989b92892093c1b50672156fa5f873c22892701cb2e207',
  );
});

test('rejects unsupported bridge methods', async () => {
  await assert.rejects(
    handleRequest({ method: 'deleteEverything' }, {}),
    /Unsupported Sui SDK method/,
  );
});

test('redacts private key material in errors', () => {
  const privateKey = 'ab'.repeat(32);
  const sanitized = sanitizeError(
    new Error(`bad key ${privateKey} suiprivkey1verysecret`),
  );

  assert.doesNotMatch(sanitized, /ababab/);
  assert.doesNotMatch(sanitized, /verysecret/);
  assert.match(sanitized, /redacted/);
});
