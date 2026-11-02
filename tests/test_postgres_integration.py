"""Optional integration check; only permits the disposable local study_test DB."""
import io
import os
import secrets
import unittest
from datetime import datetime

from PIL import Image

from app import KST, create_app
from persistence import PostgresConnection


@unittest.skipUnless(os.environ.get('STUDY_TEST_DATABASE_URL'), 'Requires disposable local PostgreSQL')
class PostgreSQLIntegrationTests(unittest.TestCase):
    def test_accounts_photos_fines_and_sessions_survive_recreation(self):
        from psycopg.conninfo import conninfo_to_dict
        dsn = os.environ['STUDY_TEST_DATABASE_URL']
        options = conninfo_to_dict(dsn)
        self.assertIn(options.get('host'), ('localhost', '127.0.0.1'))
        self.assertEqual(options.get('dbname'), 'study_test')
        connection = PostgresConnection(dsn)
        with connection:
            connection.execute('DROP SCHEMA IF EXISTS study_app CASCADE')
        connection.close()

        class Photos:
            blobs = {}
            def put(self, name, blob): self.blobs[name] = blob
            def get(self, name): return self.blobs[name]
            def delete(self, name): self.blobs.pop(name, None)

        photos = Photos()
        clock = [datetime(2026, 10, 5, 12, tzinfo=KST)]
        password = secrets.token_urlsafe(24)
        config = {'TESTING': True, 'DATABASE_URL': dsn, 'PHOTO_STORE': photos,
                  'APP_SESSION_SECRET': secrets.token_urlsafe(32), 'ADMIN_PASSWORD': password,
                  'NOW': lambda: clock[0]}
        app = create_app(config)
        client = app.test_client()
        headers = {'X-CSRF-Token': client.get('/api/session').json['csrf']}
        signed = client.post('/api/register', json={'nickname': 'Student'}, headers=headers)
        self.assertEqual(signed.status_code, 201, signed.json)
        member_id = signed.json['viewer']['id']
        self.assertEqual(client.post('/api/register', json={'nickname': 'student'}, headers=headers).status_code, 409)
        self.assertEqual(client.post('/api/admin/login', json={'password': password}, headers=headers).status_code, 200)
        picture = io.BytesIO()
        Image.new('RGB', (40, 40), 'blue').save(picture, 'PNG')
        picture.seek(0)
        self.assertEqual(client.post('/api/submissions', data={'photo': (picture, 'proof.png')}, headers=headers).status_code, 201)
        submission = client.get('/api/state').json['members'][0]['days'][0]['submission']
        self.assertEqual(client.get(submission['photo_url']).data, next(iter(photos.blobs.values())))
        self.assertEqual(client.post('/api/admin/submissions/' + str(submission['id']), json={'status': 'approved'}, headers=headers).status_code, 200)
        clock[0] = datetime(2026, 10, 12, 12, tzinfo=KST)
        week = '2026-10-05'
        settled = client.get('/api/state?week=' + week).json['members'][0]
        self.assertEqual(settled['fine'], 5000)
        self.assertEqual(client.put('/api/admin/payments', json={'member_id': member_id, 'week': week, 'paid': True}, headers=headers).status_code, 200)
        rebuilt = create_app(config).test_client()
        rebuilt.set_cookie('session', client.get_cookie('session').value)
        state = rebuilt.get('/api/state?week=' + week).json
        self.assertTrue(state['viewer']['is_admin'])
        self.assertTrue(state['members'][0]['paid'])
        self.assertEqual(state['members'][0]['approved_count'], 1)
        self.assertEqual(rebuilt.get(submission['photo_url']).status_code, 200)
        self.assertEqual(rebuilt.get('/api/admin/members').json[0]['code'], signed.json['code'])


if __name__ == '__main__':
    unittest.main()
