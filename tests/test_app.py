import io
import json
import secrets
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import KST, create_app, three_months


class StudyAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.instant = datetime(2026, 10, 5, 12, tzinfo=KST)  # Monday
        self.password = secrets.token_urlsafe(24)
        self.config = dict(TESTING=True, DATA_DIR=self.directory.name,
                           ADMIN_PASSWORD=self.password, NOW=lambda: self.instant)
        self.environment = patch.dict('os.environ', {'STUDY_VISION_API_KEY': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/session').json['csrf']

    def call(self, path, payload=None, method='post', client=None):
        target = client or self.client
        token = target.get('/api/session').json['csrf']
        return getattr(target, method)(path, json=payload or {}, headers={'X-CSRF-Token': token})

    def signup(self, name='민지', client=None):
        response = self.call('/api/register', {'nickname': name}, client=client)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json

    def admin(self):
        response = self.call('/api/admin/login', {'password': self.password})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['viewer']['is_admin'])

    def state(self, week=None, client=None):
        response = (client or self.client).get('/api/state', query_string={'week': week} if week else {})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def own(self, week=None):
        state = self.state(week)
        return next(m for m in state['members'] if m['id'] == state['viewer']['id'])

    def upload(self, color='blue', **extra):
        picture = io.BytesIO()
        Image.new('RGB', (40, 40), color).save(picture, format='PNG')
        picture.seek(0)
        response = self.client.post('/api/submissions', data={'photo': (picture, 'proof.png'), **extra},
                                    headers={'X-CSRF-Token': self.client.get('/api/session').json['csrf']})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json

    def record(self, mid, day, status='approved'):
        response = self.call('/api/admin/records', {'member_id': mid, 'date': day, 'status': status}, method='put')
        self.assertEqual(response.status_code, 200, response.json)

    def test_retirement_revokes_code_and_session_preserves_history(self):
        owner = self.signup('운영자')
        other = self.app.test_client()
        person = self.signup('참여자', client=other)
        mid = person['viewer']['id']
        path = f'/api/admin/members/{mid}'
        self.assertEqual(self.call(path, method='delete').status_code, 403)
        self.admin()
        self.record(mid, '2026-10-05')
        self.assertEqual(self.call(f"/api/admin/members/{owner['viewer']['id']}", method='delete').status_code, 400)
        self.instant += timedelta(days=2)
        self.assertEqual(self.call(path, method='delete').status_code, 200)
        self.assertIsNone(other.get('/api/session').json['viewer'])
        self.assertEqual(self.call('/api/login', {'code': person['code']}, client=other).status_code, 401)
        self.assertEqual(self.call('/api/profile', {'nickname': '변경'}, method='patch', client=other).status_code, 401)
        ended = next(m for m in self.state()['members'] if m['id'] == mid)
        self.assertEqual(ended['approved_count'], 1)
        self.assertEqual(ended['target_days'], 2)
        self.assertTrue(all(d['status'] == 'inactive' for d in ended['days'][3:]))
        codes = self.client.get('/api/admin/members').json
        self.assertIsNone(next(m for m in codes if m['id'] == mid)['code'])
        self.instant += timedelta(days=7)
        self.assertNotIn(mid, [m['id'] for m in self.state()['members']])
        historical = next(m for m in self.state('2026-10-05')['members'] if m['id'] == mid)
        self.assertEqual(historical['fine'], 1000)

    def test_signup_auth_public_privacy_and_csrf(self):
        self.assertEqual(self.client.post('/api/register', json={'nickname': 'x'}).status_code, 403)
        registered = self.signup()
        self.assertRegex(registered['code'], r'^\d{4}$')
        public = self.app.test_client()
        state = self.state(client=public)
        self.assertIsNone(state['viewer'])
        self.assertNotIn('code', json.dumps(state))
        self.assertEqual(public.get('/api/admin/members').status_code, 401)
        self.assertEqual(self.client.get('/api/admin/members').status_code, 403)
        self.admin()
        self.assertEqual(self.client.get('/api/admin/members').json[0]['code'], registered['code'])
        self.assertEqual(self.call('/api/admin/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/members').status_code, 403)
        self.assertEqual(self.state()['viewer']['id'], registered['viewer']['id'])
        self.assertEqual(self.call('/api/logout').status_code, 200)
        self.assertIsNone(self.state()['viewer'])
        self.assertEqual(self.call('/api/login', {'code': registered['code']}).status_code, 200)
        self.assertEqual(self.state()['viewer']['nickname'], '민지')
        self.assertFalse(self.state()['viewer']['is_admin'])

    def test_admin_session_survives_restart_and_password_change_revokes_it(self):
        self.signup()
        self.admin()
        cookie = self.client.get_cookie('session').value
        restarted = create_app(self.config).test_client()
        restarted.set_cookie('session', cookie)
        self.assertTrue(restarted.get('/api/session').json['viewer']['is_admin'])
        changed = create_app({**self.config, 'ADMIN_PASSWORD': secrets.token_urlsafe(24)}).test_client()
        changed.set_cookie('session', cookie)
        self.assertFalse(changed.get('/api/session').json['viewer']['is_admin'])
        self.assertEqual(changed.get('/api/admin/members').status_code, 403)

    def test_duplicate_nickname_and_four_digit_login_validation(self):
        self.signup()
        self.assertEqual(self.call('/api/register', {'nickname': '민지'}).status_code, 409)
        for code in ('123', '12345', '１２３４', 1234):
            with self.subTest(code=code):
                self.assertEqual(self.call('/api/login', {'code': code}).status_code, 400)
        self.assertEqual(self.call('/api/profile', {'nickname': '새별명'}, method='patch').json['viewer']['nickname'], '새별명')

    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.call('/api/login', {'code': '0000'}).status_code, 401)
        self.assertEqual(self.call('/api/login', {'code': '0000'}).status_code, 429)

    def test_upload_replacement_and_admin_still_has_personal_identity(self):
        registered = self.signup()
        self.admin()
        result = self.upload(date='2026-10-04', member_id='999')
        self.assertEqual(result['status'], 'pending')
        own = self.own()
        self.assertEqual(own['pending_count'], 1)
        self.assertEqual(own['approved_count'], 0)
        submission = own['days'][0]['submission']
        public = self.app.test_client()
        photo = public.get(submission['photo_url'])
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, 'image/jpeg')
        self.assertEqual(photo.headers['Cache-Control'], 'no-store')
        photo.close()
        first_photo = next((Path(self.directory.name) / 'photos').iterdir())
        self.upload('red')
        self.assertFalse(first_photo.exists())
        self.assertEqual(len(list((Path(self.directory.name) / 'photos').iterdir())), 1)
        self.assertEqual(self.own()['pending_count'], 1)
        self.assertEqual(self.own()['days'][0]['submission']['id'], submission['id'])
        approved = self.call(f"/api/admin/submissions/{submission['id']}", {'status': 'approved'})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(self.own()['approved_count'], 1)
        self.assertEqual(self.state()['viewer']['id'], registered['viewer']['id'])
        self.assertTrue(self.state()['viewer']['is_admin'])

    def test_week_fines_payment_and_changes_invalidate_paid_mark(self):
        mid = self.signup()['viewer']['id']
        self.admin()
        self.instant = datetime(2026, 10, 12, 0, 0, tzinfo=KST)
        for count, expected in ((4, 2000), (5, 1000), (6, 0), (7, 0)):
            with self.subTest(count=count):
                if count == 4:
                    for day in range(5, 9):
                        self.record(mid, f'2026-10-{day:02d}')
                else:
                    self.record(mid, f'2026-10-{4 + count:02d}')
                result = self.own('2026-10-05')
                self.assertEqual(result['approved_count'], count)
                self.assertEqual(result['fine'], expected)
                if count == 4:
                    paid = self.call('/api/admin/payments', {'member_id': mid, 'week': '2026-10-05', 'paid': True}, method='put')
                    self.assertEqual(paid.status_code, 200)
                    self.assertTrue(self.own('2026-10-05')['paid'])
                    self.assertEqual(self.state()['unpaid'], [])
                elif count == 5:
                    self.assertFalse(result['paid'])
                    self.assertEqual(self.state()['unpaid'], [{'week': '2026-10-05', 'amount': 1000}])
                    self.assertEqual(self.call('/api/admin/payments', {'member_id': mid, 'week': '2026-10-05', 'paid': True}, method='put').status_code, 200)
                    self.assertEqual(self.call('/api/admin/payments', {'member_id': mid, 'week': '2026-10-05', 'paid': False}, method='put').status_code, 200)
                    self.assertFalse(self.own('2026-10-05')['paid'])

    def test_pending_closed_week_is_not_payable_and_late_review_keeps_day(self):
        mid = self.signup()['viewer']['id']
        self.admin()
        self.upload()
        sid = self.own()['days'][0]['submission']['id']
        for week in ('2026-10-05', '2026-10-12'):
            if week == '2026-10-12':
                self.instant = datetime(2026, 10, 12, 0, 1, tzinfo=KST)
            response = self.call('/api/admin/payments', {'member_id': mid, 'week': '2026-10-05', 'paid': True}, method='put')
            self.assertEqual(response.status_code, 400)
        result = self.own('2026-10-05')
        self.assertEqual(result['settlement_status'], 'pending')
        self.assertIsNone(result['fine'])
        self.assertEqual(self.state()['unpaid'], [])
        self.assertEqual(self.call(f'/api/admin/submissions/{sid}', {'status': 'approved'}).status_code, 200)
        result = self.own('2026-10-05')
        self.assertEqual(result['days'][0]['status'], 'approved')
        self.assertEqual(result['fine'], 5000)
        self.assertEqual(result['settlement_status'], 'final')

    def test_midweek_join_and_automatic_rest(self):
        self.instant = datetime(2026, 10, 8, 12, tzinfo=KST)
        mid = self.signup()['viewer']['id']
        self.admin()
        self.instant = datetime(2026, 10, 12, 12, tzinfo=KST)
        self.record(mid, '2026-10-08')
        self.record(mid, '2026-10-10')
        result = self.own('2026-10-05')
        self.assertEqual(result['target_days'], 3)
        self.assertEqual(result['approved_count'], 2)
        self.assertEqual(result['fine'], 1000)
        self.assertEqual([d['status'] for d in result['days'][:3]], ['inactive'] * 3)
        self.assertEqual(sum(d['status'] == 'rest' for d in result['days']), 1)
        self.assertEqual(sum(d['status'] == 'missed' for d in result['days']), 1)

    def test_rest_override_and_record_boundaries(self):
        mid = self.signup()['viewer']['id']
        self.admin()
        self.instant = datetime(2026, 10, 8, 12, tzinfo=KST)
        self.record(mid, '2026-10-05')
        rest = {'member_id': mid, 'week': '2026-10-05', 'date': '2026-10-07'}
        self.assertEqual(self.call('/api/admin/rest', rest, method='put').status_code, 200)
        self.assertTrue(self.own()['days'][2]['rest_manual'])
        self.assertEqual(self.own()['days'][2]['status'], 'rest')
        self.assertEqual(self.call('/api/admin/rest', {**rest, 'date': None}, method='put').status_code, 200)
        self.assertFalse(any(d['rest_manual'] for d in self.own()['days']))
        for day in ('2026-10-04', '2026-10-12', '2026-10-05'):
            self.assertEqual(self.call('/api/admin/rest', {**rest, 'date': day}, method='put').status_code, 400)
        for day in ('2026-10-04', '2026-10-09'):
            self.assertEqual(self.call('/api/admin/records', {'member_id': mid, 'date': day, 'status': 'approved'}, method='put').status_code, 400)
        self.record(mid, '2026-10-05', 'absent')
        self.assertIsNone(self.own()['days'][0]['submission'])
        self.assertEqual(self.own()['approved_count'], 0)

    def test_korean_midnight_and_monday_boundary(self):
        self.instant = datetime(2026, 10, 11, 14, 59, tzinfo=timezone.utc)
        self.signup()
        self.upload()
        before = self.state()
        self.assertEqual(before['today'], '2026-10-11')
        self.assertEqual(before['current_week'], '2026-10-05')
        self.instant += timedelta(minutes=1)
        self.upload('red', date='2026-10-11')
        after = self.state()
        self.assertEqual(after['today'], '2026-10-12')
        self.assertEqual(after['current_week'], '2026-10-12')
        self.assertEqual(self.own()['days'][0]['status'], 'pending')
        self.assertEqual(self.own('2026-10-05')['days'][6]['status'], 'pending')

    def test_rejected_photo_is_public_and_retention_preserves_records(self):
        self.instant = datetime(2026, 10, 31, 12, tzinfo=KST)
        self.signup()
        self.admin()
        self.upload()
        record = self.own()['days'][5]['submission']
        self.assertEqual(self.call(f"/api/admin/submissions/{record['id']}", {'status': 'rejected', 'reason': '다른 날짜'}).status_code, 200)
        with self.client.get(record['photo_url']) as photo:
            self.assertEqual(photo.status_code, 200)
        self.assertEqual(self.own()['days'][5]['submission']['reason'], '다른 날짜')
        self.instant = datetime(2027, 1, 30, 23, 59, tzinfo=KST)
        self.assertEqual(self.app.cleanup_photos(), 0)
        self.instant += timedelta(minutes=1)
        self.assertEqual(self.client.get(record['photo_url']).status_code, 404)
        self.assertEqual(self.app.cleanup_photos(), 1)
        self.assertEqual(list((Path(self.directory.name) / 'photos').iterdir()), [])
        retained = self.own('2026-10-26')['days'][5]['submission']
        self.assertFalse(retained['has_photo'])
        self.assertEqual(retained['status'], 'rejected')
        self.assertEqual(retained['reason'], '다른 날짜')
        self.assertEqual(three_months(date(2026, 11, 30)), date(2027, 2, 28))

    def test_invalid_image_does_not_create_submission(self):
        self.signup()
        response = self.client.post('/api/submissions', data={'photo': (io.BytesIO(b'not an image'), 'photo.jpg')},
                                    headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.own()['pending_count'], 0)
        self.assertEqual(list((Path(self.directory.name) / 'photos').iterdir()), [])

    def test_vision_requires_clear_matching_date_and_outages_save_pending(self):
        self.signup()
        with patch.dict('os.environ', {'STUDY_VISION_API_KEY': 'test-placeholder'}):
            for result, status in (({'clear': True, 'latest_date': '2026-10-05'}, 'approved'),
                                   ({'clear': True, 'latest_date': '2026-10-04'}, 'pending'),
                                   ({'clear': True, 'latest_date': '2026-10-06'}, 'pending'),
                                   ({'clear': False, 'latest_date': '2026-10-05'}, 'pending')):
                response = io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(result)}}]}).encode())
                with patch('urllib.request.urlopen', return_value=response):
                    self.assertEqual(self.upload()['status'], status)
            with patch('urllib.request.urlopen', side_effect=TimeoutError()):
                self.assertEqual(self.upload()['status'], 'pending')

    def test_vision_response_after_midnight_preserves_received_day(self):
        self.instant = datetime(2026, 10, 11, 23, 59, 59, tzinfo=KST)
        self.signup()
        def slow_response(*args, **kwargs):
            self.instant += timedelta(seconds=2)
            return io.BytesIO(json.dumps({'choices': [{'message': {'content':
                '{"clear": true, "latest_date": "2026-10-11"}'}}]}).encode())
        with patch.dict('os.environ', {'STUDY_VISION_API_KEY': 'test-placeholder'}):
            with patch('urllib.request.urlopen', side_effect=slow_response):
                self.assertEqual(self.upload()['status'], 'approved')
        self.assertEqual(self.state()['today'], '2026-10-12')
        previous = self.own('2026-10-05')['days'][6]
        self.assertEqual(previous['status'], 'approved')
        self.assertTrue(previous['submission']['submitted_at'].startswith('2026-10-11'))
        self.assertEqual(self.own()['approved_count'], 0)

    def test_same_picture_on_different_day_goes_to_review(self):
        self.signup()
        with patch.dict('os.environ', {'STUDY_VISION_API_KEY': 'test-placeholder'}):
            response = io.BytesIO(json.dumps({'choices': [{'message': {'content': '{"clear": true, "latest_date": "2026-10-05"}'}}]}).encode())
            with patch('urllib.request.urlopen', return_value=response):
                self.assertEqual(self.upload()['status'], 'approved')
            self.instant += timedelta(days=1)
            with patch('urllib.request.urlopen') as remote:
                self.assertEqual(self.upload()['status'], 'pending')
                remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
