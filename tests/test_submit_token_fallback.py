import unittest
from unittest.mock import patch
from utils import reserve


class TokenFallbackTests(unittest.TestCase):
    def test_current_page_token_is_used_when_legacy_page_has_none(self):
        client = reserve(max_attempt=1)
        with patch.object(client, '_get_page_token', side_effect=[('', ''), ('fresh', 'value')]) as pages, patch.object(client, 'get_submit', return_value=True) as submit:
            self.assertTrue(client.submit(['16:00', '20:00'], '11034', ['032'], False))
            self.assertIn('/front/apps/seat/select?', pages.call_args.args[0])
            self.assertEqual(submit.call_args.kwargs['token'], 'fresh')

    def test_missing_token_does_not_submit(self):
        client = reserve(max_attempt=1)
        with patch.object(client, '_get_page_token', return_value=('', '')), patch.object(client, 'get_submit') as submit:
            with self.assertRaises(RuntimeError):
                client.submit(['16:00', '20:00'], '11034', ['032'], False)
            submit.assert_not_called()
