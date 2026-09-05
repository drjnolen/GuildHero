import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from CityLedger.sui_service import (
    _BRIDGE_STREAM_LIMIT,
    SuiCheckpoint,
    SuiGrpcService,
    parse_grpc_headers,
)


class SuiServiceHelpersTests(unittest.TestCase):
    def test_parses_provider_headers(self):
        self.assertEqual(
            parse_grpc_headers('{"x-api-key":"secret","x-network":7}'),
            {"x-api-key": "secret", "x-network": "7"},
        )

    def test_rejects_non_object_headers(self):
        with self.assertRaises(ValueError):
            parse_grpc_headers('["not", "an", "object"]')

    def test_rejects_invalid_header_json(self):
        with self.assertRaises(ValueError):
            parse_grpc_headers("{broken")

    def test_checkpoint_is_an_immutable_typed_boundary(self):
        checkpoint = SuiCheckpoint(sequence_number=42, transactions=[{"digest": "tx"}])

        self.assertEqual(checkpoint.sequence_number, 42)
        self.assertEqual(checkpoint.transactions[0]["digest"], "tx")
        with self.assertRaises(AttributeError):
            checkpoint.sequence_number = 43

    def test_checkpoint_batch_maps_ordered_bridge_results(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            service._request = AsyncMock(
                return_value={
                    "checkpoints": [
                        {"sequenceNumber": "41", "transactions": [{"digest": "a"}]},
                        {"sequenceNumber": "42", "transactions": [{"digest": "b"}]},
                    ]
                }
            )

            checkpoints = await service.get_checkpoints(range(41, 43))

            self.assertEqual(
                [checkpoint.sequence_number for checkpoint in checkpoints],
                [41, 42],
            )
            self.assertEqual(checkpoints[1].transactions[0]["digest"], "b")
            service._request.assert_awaited_once_with(
                "checkpoints",
                {"sequenceNumbers": [41, 42]},
                timeout=60.0,
            )

        asyncio.run(exercise())

    def test_maps_live_subscription_checkpoints(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            service._request = AsyncMock(
                return_value={
                    "checkpoints": [
                        {"sequenceNumber": "51", "transactions": [{"digest": "live"}]}
                    ]
                }
            )

            checkpoints = await service.get_subscribed_checkpoints(
                max_items=25,
                wait_ms=500,
            )

            self.assertEqual(checkpoints[0].sequence_number, 51)
            self.assertEqual(checkpoints[0].transactions[0]["digest"], "live")
            service._request.assert_awaited_once_with(
                "subscribedCheckpoints",
                {"maxItems": 25, "waitMs": 500},
                timeout=15.0,
            )

        asyncio.run(exercise())

    def test_stream_failure_polls_then_resumes_subscription(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            service._request = AsyncMock(side_effect=[
                RuntimeError("Sui SDK request failed: terminated"),
                {"sequenceNumber": "61"},
                {"sequenceNumber": "61", "transactions": [{"digest": "polled"}]},
                {"checkpoints": [{"sequenceNumber": "62", "transactions": []}]},
            ])
            with patch("CityLedger.sui_service.asyncio.sleep", new_callable=AsyncMock) as sleep:
                checkpoints = await service.get_subscribed_checkpoints()
                sleep.assert_awaited_once_with(5.0)
            self.assertEqual(checkpoints[0].sequence_number, 61)
            self.assertEqual(checkpoints[0].transactions[0]["digest"], "polled")
            resumed = await service.get_subscribed_checkpoints()
            self.assertEqual(resumed[0].sequence_number, 62)
            self.assertEqual([call.args[0] for call in service._request.await_args_list],
                             ["subscribedCheckpoints", "latestCheckpoint", "checkpoint", "subscribedCheckpoints"])
        asyncio.run(exercise())

    def test_stream_cancellation_does_not_poll(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            service._request = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await service.get_subscribed_checkpoints()
            self.assertEqual(service._request.await_count, 1)
        asyncio.run(exercise())

    def test_polling_failure_is_reported_for_outer_backoff(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            service._request = AsyncMock(side_effect=[RuntimeError("terminated"), RuntimeError("offline")])
            with patch("CityLedger.sui_service.asyncio.sleep", new_callable=AsyncMock):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    await service.get_subscribed_checkpoints()
        asyncio.run(exercise())

    def test_request_timeout_restarts_stuck_bridge(self):
        async def exercise():
            service = SuiGrpcService("https://example.invalid")
            process = SimpleNamespace(
                stdin=SimpleNamespace(
                    write=lambda payload: None,
                    drain=AsyncMock(),
                )
            )
            service._process = process
            service._ensure_process = AsyncMock(return_value=process)
            service.close = AsyncMock()

            with self.assertRaisesRegex(
                RuntimeError,
                "request timed out.*bridge was restarted",
            ):
                await service._request("checkpoints", timeout=0.001)

            service.close.assert_awaited_once()
            self.assertEqual(service._pending, {})

        asyncio.run(exercise())

    def test_bridge_process_accepts_large_checkpoint_batch_responses(self):
        async def exercise():
            process = SimpleNamespace(
                stdin=SimpleNamespace(),
                stdout=SimpleNamespace(readline=AsyncMock(return_value=b"")),
                stderr=SimpleNamespace(readline=AsyncMock(return_value=b"")),
                returncode=None,
                wait=AsyncMock(return_value=0),
            )
            spawn = AsyncMock(return_value=process)
            service = SuiGrpcService("https://example.invalid")

            with patch(
                "CityLedger.sui_service.asyncio.create_subprocess_exec",
                spawn,
            ):
                await service._ensure_process()
                await asyncio.gather(
                    service._reader_task,
                    service._stderr_task,
                    return_exceptions=True,
                )

            self.assertEqual(
                spawn.await_args.kwargs["limit"],
                _BRIDGE_STREAM_LIMIT,
            )
            self.assertGreater(_BRIDGE_STREAM_LIMIT, 833_680)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
