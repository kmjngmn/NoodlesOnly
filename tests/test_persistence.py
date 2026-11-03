import io
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from persistence import PostgresConnection, StorageError, SupabasePhotoStore, _NoRedirect


class PersistentStorageTests(unittest.TestCase):
    def test_supabase_connection_uses_official_ca_and_preserves_explicit_ca(self):
        with patch('psycopg.connect') as connect:
            connection = PostgresConnection('postgresql://app@aws-0-eu-west-1.pooler.supabase.com:6543/postgres')
            options = connect.call_args.kwargs
            self.assertEqual(options['sslmode'], 'verify-full')
            self.assertTrue(Path(options['sslrootcert']).is_file())
            self.assertEqual(Path(options['sslrootcert']).name, 'supabase-prod-ca-2021.crt')
            connection.close()
            PostgresConnection('postgresql://app@aws-0-eu-west-1.pooler.supabase.com/postgres?sslrootcert=/custom/ca.pem')
            self.assertEqual(connect.call_args.kwargs['sslrootcert'], '/custom/ca.pem')

    def setUp(self):
        self.store = SupabasePhotoStore('https://test-project.supabase.co', 'server-test-key')
        self.store.opener = MagicMock()
        self.name = 'a' * 48 + '.jpg'

    def response(self, data=b'{}'):
        self.store.opener.open.return_value.__enter__.return_value.read.return_value = data

    def test_private_storage_upload_read_and_delete(self):
        self.response()
        self.store.put(self.name, b'jpeg-data')
        request = self.store.opener.open.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.data, b'jpeg-data')
        self.assertEqual(request.get_header('Authorization'), 'Bearer server-test-key')
        self.assertEqual(request.get_header('Apikey'), 'server-test-key')
        self.assertEqual(request.get_header('Content-type'), 'image/jpeg')
        self.assertEqual(request.full_url, f'https://test-project.supabase.co/storage/v1/object/proofs/{self.name}')
        self.response(b'jpeg-data')
        self.assertEqual(self.store.get(self.name), b'jpeg-data')
        self.assertIn('/object/authenticated/proofs/', self.store.opener.open.call_args.args[0].full_url)
        self.store.delete(self.name)
        request = self.store.opener.open.call_args.args[0]
        self.assertEqual(request.method, 'DELETE')
        self.assertEqual(json.loads(request.data), {'prefixes': [self.name]})

    def test_errors_hide_remote_credentials_and_missing_deletes_are_idempotent(self):
        for status in (400, 403, 429, 500):
            self.store.opener.open.side_effect = urllib.error.HTTPError(
                'https://test-project.supabase.co', status, 'server-test-key', {}, io.BytesIO(b'private-data'))
            with self.assertRaises(StorageError) as error:
                self.store.put(self.name, b'jpeg-data')
            self.assertNotIn('server-test-key', str(error.exception))
            self.assertNotIn('private-data', str(error.exception))
        self.store.opener.open.side_effect = urllib.error.HTTPError('url', 404, 'missing', {}, None)
        self.store.delete(self.name)
        with self.assertRaises(FileNotFoundError):
            self.store.get(self.name)
        self.store.opener.open.side_effect = urllib.error.URLError('server-test-key')
        with self.assertRaises(StorageError) as error:
            self.store.get(self.name)
        self.assertNotIn('server-test-key', str(error.exception))

    def test_names_urls_and_redirects_cannot_send_credentials_elsewhere(self):
        for url in ('http://test.supabase.co', 'https://evil.example',
                    'https://test.supabase.co@evil.example', 'https://test.supabase.co/path',
                    'https://test.supabase.co?target=elsewhere', 'https://test.supabase.co:8443'):
            with self.assertRaises(ValueError):
                SupabasePhotoStore(url, 'server-test-key')
        for name in ('../secret.jpg', 'a.jpg', '/etc/passwd', 'https://evil.example/photo.jpg'):
            with self.assertRaises(ValueError):
                self.store.get(name)
        request = urllib.request.Request('https://test-project.supabase.co')
        with self.assertRaises(urllib.error.HTTPError):
            _NoRedirect().redirect_request(request, None, 302, '', {}, 'https://evil.example')
        self.store.opener.open.assert_not_called()

    def test_photo_response_is_bounded(self):
        self.response(b'x' * (self.store.MAX_PHOTO_BYTES + 1))
        with self.assertRaises(StorageError):
            self.store.get(self.name)

    def test_postgres_bound_values_and_write_transaction_lifecycle(self):
        connection = PostgresConnection.__new__(PostgresConnection)
        connection.connection = MagicMock()
        secret_value = "name' OR 1=1 -- ?"
        with connection:
            connection.execute('UPDATE members SET nickname=? WHERE id=?', (secret_value, 2))
        calls = connection.connection.execute.call_args_list
        self.assertEqual(calls[0].args, ('SET LOCAL search_path = study_app',))
        self.assertEqual(calls[2].args, ('SELECT pg_advisory_xact_lock(%s)', (connection.WRITE_LOCK,)))
        self.assertEqual(calls[3].args, ('UPDATE members SET nickname=%s WHERE id=%s', (secret_value, 2)))
        connection.connection.commit.assert_called_once()
        connection.connection.close.assert_not_called()
        with self.assertRaises(ValueError):
            with connection:
                raise ValueError('rollback')
        connection.connection.rollback.assert_called_once()
        self.assertEqual(sum(c.args[0] == 'SET LOCAL search_path = study_app'
                             for c in connection.connection.execute.call_args_list), 2)

    def test_schema_initialization_uses_private_schema_under_lock(self):
        connection = PostgresConnection.__new__(PostgresConnection)
        connection.connection = MagicMock()
        connection.setup_schema()
        calls = connection.connection.execute.call_args_list
        self.assertIn('pg_advisory_xact_lock', calls[2].args[0])
        self.assertIn('pg_namespace', calls[3].args[0])
        schema = calls[4].args[0]
        self.assertNotIn('CREATE SCHEMA', schema)
        self.assertIn('REVOKE ALL ON SCHEMA study_app FROM PUBLIC', schema)
        self.assertNotIn('DROP ', schema)
        connection.connection.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
