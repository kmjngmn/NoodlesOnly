import io
import secrets
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from PIL import Image

from app import KST, create_app
from persistence import StorageError


class ServerlessTests(unittest.TestCase):
    def test_serverless_refuses_temporary_database_fallback(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing deployment settings'):
            create_app({'SERVERLESS': True, 'DATABASE_URL': None})

    def test_cleanup_requires_cron_auth_and_retries_photo_deletion(self):
        class PhotoStore:
            blob = None
            failing = True

            def put(self, name, blob):
                self.blob = blob

            def get(self, name):
                return self.blob

            def delete(self, name):
                if self.failing:
                    raise StorageError('unavailable')
                self.blob = None

        with tempfile.TemporaryDirectory() as directory:
            clock = [datetime(2026, 1, 31, 12, tzinfo=KST)]
            store = PhotoStore()
            secret = secrets.token_urlsafe(32)
            app = create_app({'TESTING': True, 'DATA_DIR': directory, 'PHOTO_STORE': store,
                              'NOW': lambda: clock[0], 'CRON_SECRET': secret})
            client = app.test_client()
            csrf = client.get('/api/session').json['csrf']
            headers = {'X-CSRF-Token': csrf}
            client.post('/api/register', json={'nickname': '테스트'}, headers=headers)
            picture = io.BytesIO()
            Image.new('RGB', (40, 40), 'blue').save(picture, 'PNG')
            picture.seek(0)
            self.assertEqual(client.post('/api/submissions', data={'photo': (picture, 'proof.png')}, headers=headers).status_code, 201)
            photo = client.get('/api/state').json['members'][0]['days'][5]['submission']['photo_url']
            response = client.get(photo)
            self.assertEqual(response.status_code, 200)
            response.close()
            clock[0] = datetime(2026, 4, 30, 12, tzinfo=KST)
            self.assertEqual(client.get(photo).status_code, 404)
            self.assertEqual(client.get('/api/cron/cleanup').status_code, 403)
            cron_headers = {'Authorization': 'Bearer ' + secret}
            self.assertEqual(client.get('/api/cron/cleanup', headers=cron_headers).status_code, 503)
            store.failing = False
            self.assertEqual(client.get('/api/cron/cleanup', headers=cron_headers).json['deleted'], 1)
            self.assertIsNone(store.blob)
            self.assertEqual(client.get('/api/cron/cleanup', headers=cron_headers).json['deleted'], 0)


if __name__ == '__main__':
    unittest.main()
