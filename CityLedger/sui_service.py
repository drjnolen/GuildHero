"""Async access to the official Sui TypeScript SDK bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SUI_GRPC_URL = "https://fullnode.mainnet.sui.io:443"
_DEFAULT_REQUEST_TIMEOUT = 45.0
_BRIDGE_PATH = Path(__file__).with_name("sui_bridge.mjs")


def parse_grpc_headers(raw_headers: str | None) -> dict[str, str]:
    if not raw_headers:
        return {}
    try:
        parsed = json.loads(raw_headers)
    except json.JSONDecodeError as exc:
        raise ValueError("SUI_GRPC_HEADERS_JSON must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SUI_GRPC_HEADERS_JSON must be a JSON object.")
    return {str(key): str(value) for key, value in parsed.items()}


@dataclass(frozen=True)
class SuiCheckpoint:
    sequence_number: int
    transactions: list[dict[str, Any]]


class SuiGrpcService:
    """Own a persistent official-SDK process and multiplex JSON-line requests."""

    def __init__(self, grpc_url: str, headers: dict[str, str] | None = None):
        self.grpc_url = grpc_url
        self.headers = headers or {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._request_id = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._transfer_lock = asyncio.Lock()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return self._process

            environment = os.environ.copy()
            environment["GUILDHERO_SUI_GRPC_URL"] = self.grpc_url
            environment["GUILDHERO_SUI_GRPC_HEADERS"] = json.dumps(self.headers)
            node_binary = os.environ.get("SUI_NODE_BINARY", "node")
            try:
                process = await asyncio.create_subprocess_exec(
                    node_binary,
                    str(_BRIDGE_PATH),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "Node.js 22+ is required for the official Sui SDK bridge."
                ) from exc

            self._process = process
            self._reader_task = asyncio.create_task(
                self._read_responses(process),
                name="sui-sdk-response-reader",
            )
            self._stderr_task = asyncio.create_task(
                self._read_stderr(process),
                name="sui-sdk-stderr-reader",
            )
            return process

    async def _read_responses(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("Sui SDK bridge emitted an invalid response.")
                    continue
                request_id = str(response.get("id", ""))
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if response.get("error"):
                    future.set_exception(
                        RuntimeError(f"Sui SDK request failed: {response['error']}")
                    )
                else:
                    future.set_result(response.get("result"))
        finally:
            return_code = await process.wait()
            error = RuntimeError(
                f"Sui SDK bridge stopped unexpectedly (exit code {return_code})."
            )
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            if self._process is process:
                self._process = None

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while line := await process.stderr.readline():
            message = line.decode("utf-8", errors="replace").strip()
            if message:
                logging.warning("Sui SDK bridge: %s", message[:500])

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        process = await self._ensure_process()
        if process.stdin is None:
            raise RuntimeError("Sui SDK bridge input is unavailable.")

        self._request_id += 1
        request_id = str(self._request_id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

        try:
            async with self._write_lock:
                process.stdin.write(payload)
                await process.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

    async def get_latest_checkpoint(self) -> SuiCheckpoint:
        result = await self._request("latestCheckpoint", timeout=30.0)
        return SuiCheckpoint(
            sequence_number=int(result["sequenceNumber"]),
            transactions=[],
        )

    async def get_checkpoint(self, sequence_number: int) -> SuiCheckpoint:
        result = await self._request(
            "checkpoint",
            {"sequenceNumber": int(sequence_number)},
            timeout=45.0,
        )
        return SuiCheckpoint(
            sequence_number=int(result["sequenceNumber"]),
            transactions=list(result.get("transactions") or []),
        )

    async def get_checkpoints(
        self,
        sequence_numbers: list[int] | range,
    ) -> list[SuiCheckpoint]:
        """Fetch an ordered checkpoint batch through bounded bridge concurrency."""

        requested = [int(sequence_number) for sequence_number in sequence_numbers]
        if not requested:
            return []
        result = await self._request(
            "checkpoints",
            {"sequenceNumbers": requested},
            timeout=60.0,
        )
        return [
            SuiCheckpoint(
                sequence_number=int(checkpoint["sequenceNumber"]),
                transactions=list(checkpoint.get("transactions") or []),
            )
            for checkpoint in result.get("checkpoints") or []
        ]

    async def get_balance(self, owner: str, coin_type: str) -> int:
        result = await self._request(
            "balance",
            {"owner": owner, "coinType": coin_type},
            timeout=30.0,
        )
        return int(result["balance"])

    async def get_coin_metadata(self, coin_type: str) -> dict | None:
        result = await self._request(
            "coinMetadata",
            {"coinType": coin_type},
            timeout=30.0,
        )
        return result.get("coinMetadata")

    async def transfer_token(
        self,
        recipient: str,
        amount: int,
        coin_type: str,
        sender_private_key_hex: str,
        gas_budget: int,
    ) -> dict:
        """Build, sign, and execute one programmable transaction over gRPC."""

        async with self._transfer_lock:
            return await self._request(
                "transfer",
                {
                    "recipient": recipient,
                    "amount": str(amount),
                    "coinType": coin_type,
                    "privateKeyHex": sender_private_key_hex,
                    "gasBudget": str(gas_budget),
                },
                timeout=90.0,
            )


_service: SuiGrpcService | None = None


def get_sui_service(
    grpc_url: str,
    headers: dict[str, str] | None = None,
) -> SuiGrpcService:
    global _service
    if _service is None:
        _service = SuiGrpcService(grpc_url, headers)
    elif _service.grpc_url != grpc_url or _service.headers != (headers or {}):
        logging.warning(
            "Sui gRPC configuration changed after client initialization; "
            "using the existing client."
        )
    return _service


async def close_sui_service() -> None:
    global _service
    if _service is not None:
        await _service.close()
    _service = None
