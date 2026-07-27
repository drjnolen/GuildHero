import unittest

from CityLedger.sui_service import SuiCheckpoint, parse_grpc_headers


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


if __name__ == "__main__":
    unittest.main()
