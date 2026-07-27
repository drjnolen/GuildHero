import unittest
from unittest.mock import AsyncMock

from CityLedger.sui_service import SuiCheckpoint, SuiGrpcService, parse_grpc_headers


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

        import asyncio

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
