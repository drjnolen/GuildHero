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
const CHECKPOINT_BATCH_CONCURRENCY = 10;

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
      const { response } = await client.ledgerService.getCheckpoint({
        checkpointId: {
          oneofKind: 'sequenceNumber',
          sequenceNumber,
        },
        readMask: CHECKPOINT_READ_MASK,
      });
      return mapCheckpoint(response.checkpoint);
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
        async (sequenceNumber) => {
          const { response } = await client.ledgerService.getCheckpoint({
            checkpointId: {
              oneofKind: 'sequenceNumber',
              sequenceNumber,
            },
            readMask: CHECKPOINT_READ_MASK,
          });
          return mapCheckpoint(response.checkpoint);
        },
      );
      return { checkpoints };
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
