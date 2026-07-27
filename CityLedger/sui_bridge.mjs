import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';

import { SuiGrpcClient } from '@mysten/sui/grpc';
import { Ed25519Keypair } from '@mysten/sui/keypairs/ed25519';
import { Transaction } from '@mysten/sui/transactions';

const CHECKPOINT_READ_MASK = {
  paths: [
    'sequence_number',
    'transactions.digest',
    'transactions.transaction.sender',
    'transactions.transaction.kind.programmable_transaction.commands.move_call.package',
    'transactions.transaction.kind.programmable_transaction.commands.move_call.module',
    'transactions.transaction.kind.programmable_transaction.commands.move_call.function',
    'transactions.effects.status',
    'transactions.effects.gas_used',
    'transactions.effects.gas_object.input_owner',
    'transactions.events.events.package_id',
    'transactions.events.events.module',
    'transactions.events.events.event_type',
    'transactions.events.events.sender',
    'transactions.checkpoint',
    'transactions.timestamp',
    'transactions.balance_changes',
  ],
};

const CHECKPOINT_BATCH_LIMIT = 50;
const CHECKPOINT_BATCH_CONCURRENCY = 5;
const CHECKPOINT_REQUEST_TIMEOUT_MS = 10_000;
const CHECKPOINT_REQUEST_ATTEMPTS = 3;
const CHECKPOINT_RETRY_DELAY_MS = 250;
const SUBSCRIPTION_BATCH_LIMIT = 100;
const SUBSCRIPTION_QUEUE_LIMIT = 500;
const SUBSCRIPTION_WAIT_LIMIT_MS = 5_000;
const checkpointSubscriptionStates = new WeakMap();

function requiredString(value, name) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${name} is required.`);
  }
  return value.trim();
}

function requiredUnsignedInteger(value, name, { allowZero = false } = {}) {
  let parsed;
  try {
    parsed = BigInt(value);
  } catch {
    throw new Error(`${name} must be an unsigned integer.`);
  }
  if (parsed < 0n || (!allowZero && parsed === 0n)) {
    throw new Error(`${name} must be ${allowZero ? 'non-negative' : 'greater than zero'}.`);
  }
  return parsed;
}

async function mapWithConcurrency(values, concurrency, callback) {
  const results = new Array(values.length);
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < values.length) {
      const index = nextIndex;
      nextIndex += 1;
      results[index] = await callback(values[index], index);
    }
  }

  await Promise.all(
    Array.from(
      { length: Math.min(concurrency, values.length) },
      () => worker(),
    ),
  );
  return results;
}

async function getCheckpointWithRetry(client, sequenceNumber) {
  let lastError;
  for (let attempt = 1; attempt <= CHECKPOINT_REQUEST_ATTEMPTS; attempt += 1) {
    try {
      const { response } = await client.ledgerService.getCheckpoint(
        {
          checkpointId: {
            oneofKind: 'sequenceNumber',
            sequenceNumber,
          },
          readMask: CHECKPOINT_READ_MASK,
        },
        { timeout: CHECKPOINT_REQUEST_TIMEOUT_MS },
      );
      return mapCheckpoint(response.checkpoint);
    } catch (error) {
      lastError = error;
      if (attempt < CHECKPOINT_REQUEST_ATTEMPTS) {
        await new Promise((resolve) =>
          setTimeout(resolve, CHECKPOINT_RETRY_DELAY_MS * attempt),
        );
      }
    }
  }
  throw new Error(
    `Checkpoint ${sequenceNumber} failed after ${CHECKPOINT_REQUEST_ATTEMPTS} attempts: ${sanitizeError(lastError)}`,
  );
}

function createCheckpointSubscription(client) {
  const state = {
    queue: [],
    waiters: [],
    error: null,
    pump: null,
  };
  checkpointSubscriptionStates.set(client, state);
  const call = client.subscriptionService.subscribeCheckpoints({
    readMask: CHECKPOINT_READ_MASK,
  });

  state.pump = (async () => {
    try {
      for await (const response of call.responses) {
        if (!response.checkpoint) {
          continue;
        }
        const checkpoint = mapCheckpoint(response.checkpoint);
        const waiter = state.waiters.shift();
        if (waiter) {
          clearTimeout(waiter.timer);
          waiter.resolve(checkpoint);
        } else {
          state.queue.push(checkpoint);
          if (state.queue.length > SUBSCRIPTION_QUEUE_LIMIT) {
            state.queue.splice(
              0,
              state.queue.length - SUBSCRIPTION_QUEUE_LIMIT,
            );
          }
        }
      }
      throw new Error('Sui checkpoint subscription ended unexpectedly.');
    } catch (error) {
      state.error = error;
      for (const waiter of state.waiters.splice(0)) {
        clearTimeout(waiter.timer);
        waiter.reject(error);
      }
    } finally {
      if (checkpointSubscriptionStates.get(client) === state) {
        checkpointSubscriptionStates.delete(client);
      }
    }
  })();

  return state;
}

function waitForSubscribedCheckpoint(state, waitMs) {
  if (state.queue.length > 0) {
    return Promise.resolve(state.queue.shift());
  }
  if (state.error) {
    return Promise.reject(state.error);
  }
  return new Promise((resolve, reject) => {
    const waiter = { resolve, reject, timer: null };
    waiter.timer = setTimeout(() => {
      const index = state.waiters.indexOf(waiter);
      if (index >= 0) {
        state.waiters.splice(index, 1);
      }
      resolve(null);
    }, waitMs);
    state.waiters.push(waiter);
  });
}

async function getSubscribedCheckpoints(client, maxItems, waitMs) {
  const state =
    checkpointSubscriptionStates.get(client) ??
    createCheckpointSubscription(client);
  const first = await waitForSubscribedCheckpoint(state, waitMs);
  if (!first) {
    return [];
  }

  // Give the subscription pump one turn to enqueue any already-buffered items.
  await new Promise((resolve) => setTimeout(resolve, 0));
  const checkpoints = [first];
  while (checkpoints.length < maxItems && state.queue.length > 0) {
    checkpoints.push(state.queue.shift());
  }
  return checkpoints;
}

export function sanitizeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message
    .replace(/suiprivkey1[0-9a-z]+/gi, '[redacted private key]')
    .replace(/\b[0-9a-f]{64}\b/gi, '[redacted 32-byte value]')
    .slice(0, 500);
}

function mapMoveCalls(transaction) {
  const data = transaction?.transaction?.kind?.data;
  if (data?.oneofKind !== 'programmableTransaction') {
    return [];
  }
  return (data.programmableTransaction?.commands ?? [])
    .map((command) => command?.command)
    .filter((command) => command?.oneofKind === 'moveCall')
    .map((command) => ({
      move_call: {
        package: command.moveCall?.package ?? '',
        module: command.moveCall?.module ?? '',
        function: command.moveCall?.function ?? '',
      },
    }));
}

export function mapCheckpoint(checkpoint) {
  if (!checkpoint || checkpoint.sequenceNumber === undefined) {
    throw new Error('Sui gRPC returned an incomplete checkpoint.');
  }
  return {
    sequenceNumber: checkpoint.sequenceNumber.toString(),
    transactions: (checkpoint.transactions ?? []).map((item) => ({
      digest: item.digest ?? '',
      transaction: {
        sender: item.transaction?.sender ?? '',
        kind: {
          programmable_transaction: {
            commands: mapMoveCalls(item),
          },
        },
      },
      effects: {
        status: {
          success: item.effects?.status?.success ?? false,
          error: item.effects?.status?.error
            ? item.effects.status.error.message ??
              (typeof item.effects.status.error === 'string'
                ? item.effects.status.error
                : 'Transaction failed')
            : null,
        },
        gas_used: {
          computation_cost:
            item.effects?.gasUsed?.computationCost?.toString() ?? '0',
          storage_cost:
            item.effects?.gasUsed?.storageCost?.toString() ?? '0',
          storage_rebate:
            item.effects?.gasUsed?.storageRebate?.toString() ?? '0',
        },
        gas_payer: item.effects?.gasObject?.inputOwner?.address ?? '',
      },
      events: {
        events: (item.events?.events ?? []).map((event) => ({
          package_id: event.packageId ?? '',
          module: event.module ?? '',
          event_type: event.eventType ?? '',
          sender: event.sender ?? '',
        })),
      },
      checkpoint:
        item.checkpoint === undefined ? null : Number(item.checkpoint),
      timestamp: item.timestamp
        ? {
            seconds: item.timestamp.seconds?.toString() ?? null,
            nanos: item.timestamp.nanos ?? 0,
          }
        : null,
      balance_changes: (item.balanceChanges ?? []).map((change) => ({
        address: change.address ?? '',
        coin_type: change.coinType ?? '',
        amount: change.amount ?? '0',
      })),
    })),
  };
}

export function createClient(url, headers = {}) {
  return new SuiGrpcClient({
    network: 'mainnet',
    baseUrl: requiredString(url, 'Sui gRPC URL'),
    fetchInit: {
      headers,
    },
  });
}

export function buildTransferTransaction(params) {
  const transaction = new Transaction();
  transaction.transferObjects(
    [
      transaction.coin({
        balance: requiredUnsignedInteger(params.amount, 'amount'),
        type: requiredString(params.coinType, 'coinType'),
      }),
    ],
    requiredString(params.recipient, 'recipient'),
  );
  transaction.setGasBudget(
    requiredUnsignedInteger(params.gasBudget, 'gasBudget'),
  );
  return transaction;
}

export function keypairFromPrivateKeyHex(privateKeyHex) {
  const normalized = requiredString(privateKeyHex, 'privateKeyHex');
  if (!/^[0-9a-f]{64}$/i.test(normalized)) {
    throw new Error('privateKeyHex must contain a 32-byte Ed25519 seed.');
  }
  return Ed25519Keypair.fromSecretKey(
    Uint8Array.from(Buffer.from(normalized, 'hex')),
  );
}

export async function handleRequest(request, client) {
  const params = request?.params ?? {};
  switch (request?.method) {
    case 'latestCheckpoint': {
      const { response } = await client.ledgerService.getServiceInfo({});
      if (response.checkpointHeight === undefined) {
        throw new Error('Sui gRPC did not return a checkpoint height.');
      }
      return { sequenceNumber: response.checkpointHeight.toString() };
    }
    case 'checkpoint': {
      const sequenceNumber = requiredUnsignedInteger(
        params.sequenceNumber,
        'sequenceNumber',
        { allowZero: true },
      );
      return getCheckpointWithRetry(client, sequenceNumber);
    }
    case 'checkpoints': {
      if (!Array.isArray(params.sequenceNumbers) || params.sequenceNumbers.length === 0) {
        throw new Error('sequenceNumbers must be a non-empty array.');
      }
      if (params.sequenceNumbers.length > CHECKPOINT_BATCH_LIMIT) {
        throw new Error(
          `sequenceNumbers cannot contain more than ${CHECKPOINT_BATCH_LIMIT} checkpoints.`,
        );
      }
      const sequenceNumbers = params.sequenceNumbers.map((value) =>
        requiredUnsignedInteger(value, 'sequenceNumber', { allowZero: true }),
      );
      const checkpoints = await mapWithConcurrency(
        sequenceNumbers,
        CHECKPOINT_BATCH_CONCURRENCY,
        (sequenceNumber) => getCheckpointWithRetry(client, sequenceNumber),
      );
      return { checkpoints };
    }
    case 'subscribedCheckpoints': {
      const maxItems = Number(
        requiredUnsignedInteger(
          params.maxItems ?? CHECKPOINT_BATCH_LIMIT,
          'maxItems',
        ),
      );
      if (maxItems > SUBSCRIPTION_BATCH_LIMIT) {
        throw new Error(
          `maxItems cannot be greater than ${SUBSCRIPTION_BATCH_LIMIT}.`,
        );
      }
      const waitMs = Number(
        requiredUnsignedInteger(
          params.waitMs ?? 1_000,
          'waitMs',
          { allowZero: true },
        ),
      );
      if (waitMs > SUBSCRIPTION_WAIT_LIMIT_MS) {
        throw new Error(
          `waitMs cannot be greater than ${SUBSCRIPTION_WAIT_LIMIT_MS}.`,
        );
      }
      return {
        checkpoints: await getSubscribedCheckpoints(
          client,
          maxItems,
          waitMs,
        ),
      };
    }
    case 'balance': {
      const response = await client.getBalance({
        owner: requiredString(params.owner, 'owner'),
        coinType: requiredString(params.coinType, 'coinType'),
      });
      return { balance: response.balance.balance };
    }
    case 'coinMetadata': {
      const response = await client.getCoinMetadata({
        coinType: requiredString(params.coinType, 'coinType'),
      });
      return { coinMetadata: response.coinMetadata };
    }
    case 'transfer': {
      const keypair = keypairFromPrivateKeyHex(params.privateKeyHex);
      const transaction = buildTransferTransaction(params);

      const result = await keypair.signAndExecuteTransaction({
        transaction,
        client,
      });
      const executed = result.Transaction ?? result.FailedTransaction;
      if (!executed?.status?.success) {
        throw new Error(
          `Transaction failed: ${executed?.status?.error?.message ?? 'unknown execution error'}`,
        );
      }
      return { digest: executed.digest };
    }
    default:
      throw new Error(`Unsupported Sui SDK method: ${request?.method ?? 'missing'}.`);
  }
}

export async function startBridge() {
  let headers = {};
  const rawHeaders = process.env.GUILDHERO_SUI_GRPC_HEADERS;
  if (rawHeaders) {
    const parsedHeaders = JSON.parse(rawHeaders);
    if (!parsedHeaders || Array.isArray(parsedHeaders) || typeof parsedHeaders !== 'object') {
      throw new Error('GUILDHERO_SUI_GRPC_HEADERS must be a JSON object.');
    }
    headers = Object.fromEntries(
      Object.entries(parsedHeaders).map(([key, value]) => [String(key), String(value)]),
    );
  }
  const client = createClient(
    process.env.GUILDHERO_SUI_GRPC_URL ??
      'https://fullnode.mainnet.sui.io:443',
    headers,
  );
  const lines = createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });
  for await (const line of lines) {
    let request;
    try {
      request = JSON.parse(line);
      const result = await handleRequest(request, client);
      process.stdout.write(`${JSON.stringify({ id: request.id, result })}\n`);
    } catch (error) {
      process.stdout.write(
        `${JSON.stringify({
          id: request?.id ?? null,
          error: sanitizeError(error),
        })}\n`,
      );
    }
  }
}

const isMain =
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  startBridge().catch((error) => {
    process.stderr.write(`${sanitizeError(error)}\n`);
    process.exitCode = 1;
  });
}
