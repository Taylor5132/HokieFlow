import unittest
from unittest.mock import patch, Mock
from app import ui_files
try:
    from auth import register_user, require_auth, _auth_call
    from database import get_supabase_client
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False

@unittest.skipUnless(AUTH_AVAILABLE, "Install requirements.txt for auth integration tests")
class FixTests(unittest.TestCase):
    def test_sdk_models_are_converted(self):
        response = Mock()
        response.model_dump.return_value = {'user': {'id': 'one'}, 'session': None}
        client = Mock()
        client.auth.sign_up.return_value = response
        client.auth.sign_in_with_password.return_value = None
        with patch('auth.get_supabase_client', return_value=client):
            result = register_user('test@example.com', 'a-long-test-password')
        self.assertIsNone(result['session'])
        response.model_dump.assert_called_once_with(mode='json')

    def test_stream_reset_is_actionable_and_not_retried(self):
        method = Mock(side_effect=RuntimeError('<StreamReset stream_id:1, error_code:1, remote_reset:True>'))
        with self.assertRaisesRegex(ValueError, 'Please try again'):
            _auth_call(method, {})
        self.assertEqual(method.call_count, 1)

    def test_verifies_supplied_token(self):
        client = Mock()
        client.auth.get_user.return_value = {'user': {'id': 'verified'}}
        with patch('auth.get_supabase_client', return_value=client):
            self.assertEqual(require_auth({'Authorization': 'Bearer exact-token'})['id'], 'verified')
        client.auth.get_user.assert_called_once_with('exact-token')

    def test_http1_transport(self):
        with patch.dict('os.environ', {'SUPABASE_URL': 'https://example.supabase.co', 'SUPABASE_KEY': 'test-only'}), patch('database.create_client') as create, patch('database.httpx.Client') as transport:
            get_supabase_client()
            transport.assert_called_once_with(http2=False, timeout=15.0)
            self.assertFalse(create.call_args.kwargs['options'].persist_session)

    def test_live_departure_contract_and_failure(self):
        payload = {'departures': [{'route': 'HWD', 'departure_at': '2026-09-20T12:00:00-04:00'}]}
        with patch.object(ui_files.config, 'CACHE_ONLY', False), patch.object(ui_files.transit, 'departures', return_value=payload):
            self.assertEqual(ui_files.transit_departures_endpoint('1114'), payload)
        with patch.object(ui_files.config, 'CACHE_ONLY', False), patch.object(ui_files.transit, 'departures', side_effect=TimeoutError):
            self.assertEqual(ui_files.transit_departures_endpoint('1114')['status'], 'unavailable')

    def test_dining_contract(self):
        self.assertEqual(len(ui_files.dining_places_endpoint()['places']), 9)
