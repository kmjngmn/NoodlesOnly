from __future__ import annotations

import hashlib
import hmac
import io
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request, send_from_directory, session, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from persistence import PostgresConnection, SupabasePhotoStore, StorageError

KST = ZoneInfo('Asia/Seoul')
ROOT = Path(__file__).resolve().parent
PHOTO_RETENTION = timedelta(days=21)
MAX_PHOTO_BYTES = 2_000_000
Image.MAX_IMAGE_PIXELS = 25_000_000


def monday(day):
    return day - timedelta(days=day.weekday())


def create_app(config=None):
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    app.config.update(DATA_DIR=os.environ.get('DATA_DIR', str(ROOT / 'data')),
                      MAX_CONTENT_LENGTH=10 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(days=90),
                      ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD'),
                      DATABASE_URL=os.environ.get('DATABASE_URL'),
                      SUPABASE_URL=os.environ.get('SUPABASE_URL'),
                      SUPABASE_SERVICE_ROLE_KEY=os.environ.get('SUPABASE_SERVICE_ROLE_KEY'),
                      APP_SESSION_SECRET=os.environ.get('APP_SESSION_SECRET'),
                      CRON_SECRET=os.environ.get('CRON_SECRET'),
                      SERVERLESS=os.environ.get('VERCEL') == '1',
                      NOW=lambda: datetime.now(KST))
    if config:
        app.config.update(config)
    remote = bool(app.config['DATABASE_URL'])
    if app.config['SERVERLESS']:
        required = ('DATABASE_URL', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY',
                    'APP_SESSION_SECRET', 'ADMIN_PASSWORD', 'CRON_SECRET')
        missing = [name for name in required if not app.config.get(name)]
        if missing:
            raise RuntimeError('Missing deployment settings: ' + ', '.join(missing))
        app.config.update(SESSION_COOKIE_SECURE=True, MAX_CONTENT_LENGTH=4 * 1024 * 1024)
    data = Path(app.config['DATA_DIR'])
    photos = data / 'photos'
    admin_file = data / 'admin.hash'
    photo_store = app.config.get('PHOTO_STORE')
    if remote:
        if not app.config['APP_SESSION_SECRET'] or len(app.config['APP_SESSION_SECRET']) < 32:
            raise RuntimeError('APP_SESSION_SECRET must contain at least 32 characters.')
        app.secret_key = app.config['APP_SESSION_SECRET']
        photo_store = photo_store or SupabasePhotoStore(
            app.config['SUPABASE_URL'], app.config['SUPABASE_SERVICE_ROLE_KEY'])
    else:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        photos.mkdir(mode=0o700, exist_ok=True)
        key_file = data / 'session.key'
        if not key_file.exists():
            try:
                with key_file.open('x') as f:
                    f.write(secrets.token_hex(32))
                key_file.chmod(0o600)
            except FileExistsError:
                pass
        app.secret_key = key_file.read_text()
    env_password = app.config['ADMIN_PASSWORD']
    env_admin_hash = generate_password_hash(env_password) if env_password else None

    def now():
        return app.config['NOW']().astimezone(KST)

    def today():
        return now().date()

    def db():
        if 'db' not in g:
            if remote:
                g.db = PostgresConnection(app.config['DATABASE_URL'])
            else:
                g.db = sqlite3.connect(data / 'study.sqlite3', timeout=15)
                g.db.row_factory = sqlite3.Row
                g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error=None):
        if 'db' in g:
            g.pop('db').close()

    with app.app_context():
        if remote:
            db().setup_schema()
        else:
            db().executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY, nickname TEXT NOT NULL COLLATE NOCASE UNIQUE,
            code TEXT NOT NULL UNIQUE, joined_on TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
            day TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('approved','pending','rejected')),
            reason TEXT NOT NULL DEFAULT '', photo TEXT, photo_hash TEXT,
            submitted_at TEXT NOT NULL, expires_on TEXT, UNIQUE(member_id,day));
        CREATE TABLE IF NOT EXISTS rests (
            member_id INTEGER NOT NULL REFERENCES members(id), week TEXT NOT NULL,
            day TEXT NOT NULL, PRIMARY KEY(member_id,week));
        CREATE TABLE IF NOT EXISTS payments (
            member_id INTEGER NOT NULL REFERENCES members(id), week TEXT NOT NULL,
            amount INTEGER NOT NULL, paid_at TEXT NOT NULL, PRIMARY KEY(member_id,week));
        CREATE TABLE IF NOT EXISTS photo_objects (photo TEXT PRIMARY KEY, expires_on TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (bucket TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS attempt_bucket ON attempts(bucket,at);
        ''')

        with db():
            # Convert the previous three-month date-only policy for existing photos.
            for row in db().execute("SELECT id,photo,submitted_at,expires_on FROM submissions WHERE photo IS NOT NULL AND length(expires_on)=10").fetchall():
                expiry = (datetime.fromisoformat(row['submitted_at']).astimezone(KST) + PHOTO_RETENTION).isoformat()
                db().execute('UPDATE submissions SET expires_on=? WHERE id=? AND photo=? AND expires_on=?',
                             (expiry, row['id'], row['photo'], row['expires_on']))
                db().execute('UPDATE photo_objects SET expires_on=? WHERE photo=? AND expires_on>?',
                             (expiry, row['photo'], expiry))
            # Switch legacy pending records to the new post-upload review workflow.
            for row in db().execute("SELECT id,member_id,day FROM submissions WHERE status='pending'").fetchall():
                db().execute("UPDATE submissions SET status='approved',reason='' WHERE id=? AND status='pending'", (row['id'],))
                db().execute('DELETE FROM rests WHERE member_id=? AND day=?', (row['member_id'], row['day']))
                db().execute('DELETE FROM payments WHERE member_id=? AND week=?',
                             (row['member_id'], monday(date.fromisoformat(row['day'])).isoformat()))
            db().execute("""INSERT INTO photo_objects(photo,expires_on)
                            SELECT photo,expires_on FROM submissions
                            WHERE photo IS NOT NULL AND expires_on IS NOT NULL
                            ON CONFLICT(photo) DO NOTHING""")

    with app.app_context():
        with db():
            if remote:
                db().execute('ALTER TABLE members ADD COLUMN IF NOT EXISTS ended_on TEXT')
            elif 'ended_on' not in [r['name'] for r in db().execute('PRAGMA table_info(members)')]:
                db().execute('ALTER TABLE members ADD COLUMN ended_on TEXT')

    def admin_hash():
        return env_admin_hash or (admin_file.read_text().strip() if not remote and admin_file.exists() else None)

    def is_admin():
        saved = admin_hash()
        return bool(session.get('member_id') and saved and session.get('admin_until', 0) > time.time()
                    and session.get('admin_revision') == admin_revision(saved))

    def admin_revision(saved):
        return hmac.new(app.secret_key.encode(), (env_password or saved).encode(), hashlib.sha256).hexdigest()

    def viewer():
        row = db().execute('SELECT id,nickname FROM members WHERE id=? AND ended_on IS NULL', (session.get('member_id'),)).fetchone()
        return {**dict(row), 'is_admin': is_admin()} if row else None

    def require_member():
        who = viewer()
        if not who:
            fail('먼저 개인 코드로 입장해 주세요.', 401)
        return who['id']

    def require_admin():
        require_member()
        if not is_admin():
            fail('운영자 인증이 필요합니다.', 403)

    def fail(message, status=400):
        from werkzeug.exceptions import abort
        abort(status, description=message)

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            fail('입력 내용을 확인해 주세요.')
        return value

    def parse_day(value):
        try:
            result = date.fromisoformat(value)
            if result.isoformat() != value:
                raise ValueError()
            return result
        except (TypeError, ValueError):
            fail('날짜 형식이 올바르지 않습니다.')

    def parse_week(value):
        result = parse_day(value)
        if result != monday(result) or result > monday(today()):
            fail('월요일 기준의 지난주 또는 이번 주를 선택해 주세요.')
        return result

    def member(mid):
        row = db().execute('SELECT * FROM members WHERE id=?', (mid,)).fetchone()
        if not row:
            fail('회원을 찾을 수 없습니다.', 404)
        return row

    def nickname(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 20 or any(ord(c) < 32 for c in value):
            fail('별명은 1~20자로 입력해 주세요.')
        return value.strip()

    def limit(bucket, count):
        # ponytail: SQLite rate limits suit one small study; use shared limits if scaled across servers.
        key = f'{request.remote_addr}:{bucket}'
        instant = time.time()
        with db():
            db().execute('DELETE FROM attempts WHERE at<?', (instant - 900,))
            used = db().execute('SELECT COUNT(*) AS n FROM attempts WHERE bucket=?', (key,)).fetchone()['n']
            if used >= count:
                fail('시도가 너무 많습니다. 15분 후 다시 시도해 주세요.', 429)
            db().execute('INSERT INTO attempts VALUES (?,?)', (key, instant))

    def remove_photo(name):
        if photo_store:
            photo_store.delete(name)
        else:
            (photos / name).unlink(missing_ok=True)

    def discard_photo(name):
        # Keep a retry record until the physical deletion succeeds.
        with db():
            db().execute('UPDATE photo_objects SET expires_on=? WHERE photo=?', (now().isoformat(), name))
        try:
            remove_photo(name)
        except (StorageError, OSError):
            return
        with db():
            db().execute('DELETE FROM photo_objects WHERE photo=?', (name,))

    def cleanup():
        count = 0
        with app.app_context():
            with db():
                rows = db().execute('SELECT photo FROM photo_objects WHERE expires_on<=?',
                                    (now().isoformat(),)).fetchall()
                for row in rows:
                    remove_photo(row['photo'])
                    db().execute('UPDATE submissions SET photo=NULL WHERE photo=?', (row['photo'],))
                    db().execute('DELETE FROM photo_objects WHERE photo=?', (row['photo'],))
                    count += 1
        return count

    app.cleanup_photos = cleanup
    if not app.config['SERVERLESS']:
        cleanup()
        if not app.config.get('TESTING'):
            def retain():
                while True:
                    time.sleep(3600)
                    try:
                        cleanup()
                    except Exception:
                        app.logger.error('Photo retention failed; will retry.')
            threading.Thread(target=retain, daemon=True, name='photo-retention').start()

    @app.get('/api/cron/cleanup')
    def cleanup_cron():
        secret = app.config.get('CRON_SECRET')
        supplied = request.headers.get('Authorization', '')
        if not secret or not secrets.compare_digest(supplied, 'Bearer ' + secret):
            fail('접근 권한이 없습니다.', 403)
        return jsonify(deleted=cleanup())

    @app.before_request
    def csrf_check():
        if request.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            expected, supplied = session.get('csrf', ''), request.headers.get('X-CSRF-Token', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                fail('화면을 새로고침한 뒤 다시 시도해 주세요.', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.path.startswith(('/api/', '/photos/')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = '사진 용량이 너무 큽니다. 더 작은 사진을 선택해 주세요.' if error.code == 413 else error.description
        return jsonify(error=message), error.code

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error):
        db().rollback()
        return jsonify(error='이미 사용 중인 별명입니다. 다른 별명을 선택해 주세요.'), 409

    @app.errorhandler(StorageError)
    def storage_error(error):
        return jsonify(error='사진 저장소 연결을 확인한 뒤 다시 시도해 주세요.'), 503

    if remote:
        import psycopg
        app.register_error_handler(psycopg.IntegrityError, conflict)

        @app.errorhandler(psycopg.OperationalError)
        def database_error(error):
            return jsonify(error='기록 저장소에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.'), 503

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/session')
    def get_session():
        session.permanent = True
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return jsonify(viewer=viewer(), csrf=session['csrf'])

    @app.post('/api/register')
    def register():
        limit('register', 15)
        name = nickname(body().get('nickname'))
        # Serialize code allocation to keep four-digit codes unique under concurrent signups.
        db().execute('BEGIN IMMEDIATE')
        try:
            taken = {r['code'] for r in db().execute('SELECT code FROM members')}
            if len(taken) >= 100:
                fail('스터디 인원이 가득 찼습니다.')
            code = secrets.choice([f'{i:04d}' for i in range(10000) if f'{i:04d}' not in taken])
            cur = db().execute('INSERT INTO members(nickname,code,joined_on) VALUES (?,?,?) RETURNING id',
                               (name, code, today().isoformat()))
            member_id = cur.fetchone()['id']
            db().commit()
        except Exception:
            db().rollback()
            raise
        session.pop('admin_until', None)
        session['member_id'] = member_id
        return jsonify(code=code, viewer=viewer()), 201

    @app.post('/api/login')
    def login():
        limit('login', 10)
        code = body().get('code')
        if not isinstance(code, str) or len(code) != 4 or not code.isascii() or not code.isdigit():
            fail('4자리 숫자 코드를 입력해 주세요.')
        who = db().execute('SELECT id FROM members WHERE code=? AND ended_on IS NULL', (code,)).fetchone()
        if not who:
            fail('일치하는 코드를 찾지 못했습니다.', 401)
        session.pop('admin_until', None)
        session['member_id'] = who['id']
        return jsonify(viewer=viewer())

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.patch('/api/profile')
    def profile():
        mid = require_member()
        with db():
            db().execute('UPDATE members SET nickname=? WHERE id=?', (nickname(body().get('nickname')), mid))
        return jsonify(viewer=viewer())

    @app.post('/api/admin/login')
    def admin_login():
        require_member()
        limit('admin', 5)
        password = body().get('password')
        saved = admin_hash()
        if not saved:
            fail('운영자 비밀번호가 설정되지 않았습니다.', 503)
        if not isinstance(password, str) or len(password) > 256 or not check_password_hash(saved, password):
            fail('운영자 비밀번호가 일치하지 않습니다.', 401)
        session['admin_until'] = time.time() + 12 * 3600
        session['admin_revision'] = admin_revision(saved)
        return jsonify(viewer=viewer())

    @app.post('/api/admin/logout')
    def admin_logout():
        session.pop('admin_until', None)
        return jsonify(viewer=viewer())

    @app.get('/api/admin/members')
    def admin_members():
        require_admin()
        return jsonify([dict(r) for r in db().execute('SELECT id,nickname,CASE WHEN ended_on IS NULL THEN code ELSE NULL END AS code,ended_on FROM members ORDER BY id')])

    @app.delete('/api/admin/members/<int:mid>')
    def retire_member(mid):
        require_admin()
        if mid == session.get('member_id'):
            fail('현재 운영자 계정은 종료할 수 없습니다.')
        who = member(mid)
        if not who['ended_on']:
            # End today, preserving today's proof and all historical settlement records.
            with db():
                db().execute('UPDATE members SET ended_on=? WHERE id=?', (today().isoformat(), mid))
                invalidate(mid, today().isoformat())
        return jsonify(ok=True)

    def photo_dict(row):
        if not row:
            return None
        visible = bool(row['photo'] and row['expires_on'] > now().isoformat())
        return dict(id=row['id'], photo_url=f"/photos/{row['id']}" if visible else None,
                    has_photo=visible, status=row['status'], reason=row['reason'], submitted_at=row['submitted_at'])

    def week_member(who, week):
        end, current = week + timedelta(days=6), today()
        joined = date.fromisoformat(who['joined_on'])
        ended = date.fromisoformat(who['ended_on']) if who['ended_on'] else date.max
        rows = {r['day']: r for r in db().execute('SELECT * FROM submissions WHERE member_id=? AND day BETWEEN ? AND ?',
                                                 (who['id'], week.isoformat(), end.isoformat()))}
        rest_row = db().execute('SELECT day FROM rests WHERE member_id=? AND week=?', (who['id'], week.isoformat())).fetchone()
        rest = rest_row['day'] if rest_row else None
        eligible = [week + timedelta(days=i) for i in range(7) if joined <= week + timedelta(days=i) <= ended]
        target = max(0, len(eligible) - 1)
        approved = sum(r['status'] == 'approved' for r in rows.values())
        pending = sum(r['status'] == 'pending' for r in rows.values())
        missed = [d.isoformat() for d in eligible if d < current and (d.isoformat() not in rows or rows[d.isoformat()]['status'] == 'rejected')]
        if not rest and missed:
            rest = missed[0]
        closed = end < current
        fine = max(0, target - approved) * 1000 if closed and not pending else None
        elapsed = sum(d < current for d in eligible)
        past_approved = sum(r['status'] == 'approved' and date.fromisoformat(r['day']) < current for r in rows.values())
        past_pending = sum(r['status'] == 'pending' and date.fromisoformat(r['day']) < current for r in rows.values())
        provisional = max(0, target - approved - pending) * 1000 if closed else max(0, elapsed - past_approved - past_pending - 1) * 1000
        paid_row = db().execute('SELECT * FROM payments WHERE member_id=? AND week=?', (who['id'], week.isoformat())).fetchone()
        paid = bool(fine and paid_row and paid_row['amount'] == fine)
        days = []
        for i in range(7):
            day = week + timedelta(days=i)
            key = day.isoformat()
            row = rows.get(key)
            if day < joined or day > ended:
                status = 'inactive'
            elif row and row['status'] in ('approved', 'pending'):
                status = row['status']
            elif key == rest:
                status = 'rest'
            elif row:
                status = 'rejected'
            else:
                status = 'missed' if day < current else ('open' if day == current else 'upcoming')
            days.append(dict(date=key, status=status, submission=photo_dict(row), rest_manual=bool(rest_row and key == rest_row['day'])))
        return dict(id=who['id'], nickname=who['nickname'], joined_on=who['joined_on'], ended_on=who['ended_on'], days=days,
                    approved_count=approved, target_days=target, fine=fine, provisional_fine=provisional,
                    pending_count=pending, settlement_status='pending' if closed and pending else ('final' if closed else 'in_progress'),
                    paid=paid, paid_at=paid_row['paid_at'] if paid else None)

    @app.get('/api/state')
    def state():
        week = parse_week(request.args.get('week', monday(today()).isoformat()))
        people = db().execute('SELECT * FROM members ORDER BY id').fetchall()
        members = [week_member(who, week) for who in people if not who['ended_on'] or who['ended_on'] >= week.isoformat()]
        unpaid = []
        who = viewer()
        if who:
            own = member(who['id'])
            first = monday(date.fromisoformat(own['joined_on']))
            while first < monday(today()):
                settled = week_member(own, first)
                if settled['fine'] and not settled['paid']:
                    unpaid.append(dict(week=first.isoformat(), amount=settled['fine']))
                first += timedelta(days=7)
        return jsonify(today=today().isoformat(), current_week=monday(today()).isoformat(), week=week.isoformat(),
                       week_end=(week + timedelta(days=6)).isoformat(), closed=week + timedelta(days=6) < today(),
                       configured_admin=bool(admin_hash()), verification_mode='auto_approve',
                       viewer=who, members=members, unpaid=unpaid,
                       totals=dict(fine=sum(m['fine'] or 0 for m in members), paid=sum(m['fine'] or 0 for m in members if m['paid']),
                                   unpaid=sum(m['fine'] or 0 for m in members if not m['paid']), pending=sum(m['pending_count'] for m in members)))

    @app.get('/photos/<int:sid>')
    def photo(sid):
        row = db().execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()
        if not row or not row['photo'] or row['expires_on'] <= now().isoformat():
            fail('보관 기간이 지났거나 사진이 없습니다.', 404)
        if photo_store:
            try:
                return Response(photo_store.get(row['photo']), mimetype='image/jpeg')
            except FileNotFoundError:
                fail('사진을 찾을 수 없습니다.', 404)
        return send_from_directory(photos, row['photo'], mimetype='image/jpeg', conditional=False, max_age=0)

    @app.post('/api/submissions')
    def upload():
        mid = require_member()
        limit('upload', 20)
        received = now()
        day = received.date().isoformat()
        upload_file = request.files.get('photo')
        if not upload_file:
            fail('사진을 선택해 주세요.')
        try:
            with Image.open(upload_file.stream) as original:
                if original.format not in ('JPEG', 'PNG', 'WEBP'):
                    fail('JPG, PNG, WebP 사진을 선택해 주세요.')
                picture = ImageOps.exif_transpose(original).convert('RGB')
                picture.thumbnail((1800, 1800))
                for quality in (85, 75, 65, 55):
                    buffer = io.BytesIO()
                    picture.save(buffer, format='JPEG', quality=quality)
                    blob = buffer.getvalue()
                    if len(blob) <= MAX_PHOTO_BYTES:
                        break
                if len(blob) > MAX_PHOTO_BYTES:
                    fail('사진을 2MB 이하로 압축할 수 없습니다. 더 작은 사진을 선택해 주세요.', 413)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail('사진 파일을 읽을 수 없습니다. 다른 JPG 또는 PNG 파일을 선택해 주세요.')
        status, reason = 'approved', ''
        filename = secrets.token_hex(24) + '.jpg'
        with db():
            db().execute('INSERT INTO photo_objects VALUES (?,?)',
                         (filename, (received + PHOTO_RETENTION).isoformat()))
        if photo_store:
            photo_store.put(filename, blob)
        else:
            (photos / filename).write_bytes(blob)
        old = None
        try:
            with db():
                db().execute('BEGIN IMMEDIATE')
                old = db().execute('SELECT photo FROM submissions WHERE member_id=? AND day=?', (mid, day)).fetchone()
                db().execute('''INSERT INTO submissions(member_id,day,status,reason,photo,submitted_at,expires_on)
                                VALUES (?,?,?,?,?,?,?) ON CONFLICT(member_id,day) DO UPDATE SET
                                status=excluded.status,reason=excluded.reason,photo=excluded.photo,photo_hash=NULL,
                                submitted_at=excluded.submitted_at,expires_on=excluded.expires_on''',
                             (mid, day, status, reason, filename, received.isoformat(), (received + PHOTO_RETENTION).isoformat()))
                db().execute('DELETE FROM rests WHERE member_id=? AND day=?', (mid, day))
                db().execute('DELETE FROM payments WHERE member_id=? AND week=?', (mid, monday(received.date()).isoformat()))
        except Exception:
            discard_photo(filename)
            raise
        if old and old['photo']:
            discard_photo(old['photo'])
        return jsonify(status=status, reason=reason), 201

    def invalidate(mid, day):
        db().execute('DELETE FROM payments WHERE member_id=? AND week=?', (mid, monday(date.fromisoformat(day)).isoformat()))

    @app.post('/api/admin/submissions/<int:sid>')
    def review(sid):
        require_admin()
        value = body()
        status = value.get('status')
        if status not in ('approved', 'rejected'):
            fail('승인 또는 반려를 선택해 주세요.')
        reason = value.get('reason') or ('운영자 승인' if status == 'approved' else '운영자 반려')
        if not isinstance(reason, str) or len(reason) > 300:
            fail('사유는 300자 이하로 입력해 주세요.')
        with db():
            row = db().execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()
            if not row:
                fail('인증을 찾을 수 없습니다.', 404)
            db().execute('UPDATE submissions SET status=?,reason=? WHERE id=?', (status, reason, sid))
            if status == 'approved':
                db().execute('DELETE FROM rests WHERE member_id=? AND day=?', (row['member_id'], row['day']))
            invalidate(row['member_id'], row['day'])
        return jsonify(ok=True)

    @app.put('/api/admin/records')
    def edit_record():
        require_admin()
        value = body()
        who = member(value.get('member_id'))
        day = parse_day(value.get('date'))
        if not date.fromisoformat(who['joined_on']) <= day <= min(today(), date.fromisoformat(who['ended_on']) if who['ended_on'] else today()):
            fail('가입일 이후부터 오늘까지의 기록만 수정할 수 있습니다.')
        status = value.get('status')
        if status not in ('approved', 'rejected', 'absent'):
            fail('기록 상태가 올바르지 않습니다.')
        reason = value.get('reason') or '운영자 기록 수정'
        if not isinstance(reason, str) or len(reason) > 300:
            fail('사유는 300자 이하로 입력해 주세요.')
        old_photo = None
        with db():
            if status == 'absent':
                old = db().execute('SELECT photo FROM submissions WHERE member_id=? AND day=?', (who['id'], day.isoformat())).fetchone()
                old_photo = old['photo'] if old else None
                db().execute('DELETE FROM submissions WHERE member_id=? AND day=?', (who['id'], day.isoformat()))
            else:
                db().execute('''INSERT INTO submissions(member_id,day,status,reason,submitted_at) VALUES (?,?,?,?,?)
                                ON CONFLICT(member_id,day) DO UPDATE SET status=excluded.status,reason=excluded.reason''',
                             (who['id'], day.isoformat(), status, reason, now().isoformat()))
            if status == 'approved':
                db().execute('DELETE FROM rests WHERE member_id=? AND day=?', (who['id'], day.isoformat()))
            invalidate(who['id'], day.isoformat())
        if old_photo:
            discard_photo(old_photo)
        return jsonify(ok=True)

    @app.put('/api/admin/rest')
    def edit_rest():
        require_admin()
        value = body()
        who = member(value.get('member_id'))
        week = parse_week(value.get('week'))
        day = parse_day(value['date']) if value.get('date') else None
        if day:
            if not week <= day <= week + timedelta(days=6) or day < date.fromisoformat(who['joined_on']) or (who['ended_on'] and day.isoformat() > who['ended_on']):
                fail('해당 주의 가입일 이후 날짜를 선택해 주세요.')
            existing = db().execute('SELECT status FROM submissions WHERE member_id=? AND day=?', (who['id'], day.isoformat())).fetchone()
            if existing and existing['status'] in ('approved', 'pending'):
                fail('인증이 있거나 확인 중인 날은 휴식일로 지정할 수 없습니다.')
        with db():
            db().execute('DELETE FROM rests WHERE member_id=? AND week=?', (who['id'], week.isoformat()))
            if day:
                db().execute('INSERT INTO rests VALUES (?,?,?)', (who['id'], week.isoformat(), day.isoformat()))
        return jsonify(ok=True)

    @app.put('/api/admin/payments')
    def payment():
        require_admin()
        value = body()
        who = member(value.get('member_id'))
        week = parse_week(value.get('week'))
        if type(value.get('paid')) is not bool:
            fail('납부 여부를 확인해 주세요.')
        # Read and write under the same lock so concurrent review cannot mark a stale amount paid.
        with db():
            db().execute('BEGIN IMMEDIATE')
            settled = week_member(who, week)
            if settled['settlement_status'] != 'final' or not settled['fine']:
                fail('확정된 벌금이 있는 지난주 내역만 납부 처리할 수 있습니다.')
            if value['paid']:
                db().execute('''INSERT INTO payments VALUES (?,?,?,?) ON CONFLICT(member_id,week)
                                DO UPDATE SET amount=excluded.amount,paid_at=excluded.paid_at''',
                             (who['id'], week.isoformat(), settled['fine'], now().isoformat()))
            else:
                db().execute('DELETE FROM payments WHERE member_id=? AND week=?', (who['id'], week.isoformat()))
        return jsonify(ok=True)

    return app


if __name__ == '__main__':
    import argparse
    import getpass
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', choices=['serve', 'init-admin', 'cleanup'], default='serve')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.command == 'init-admin':
        password = getpass.getpass('운영자 비밀번호 (10자 이상): ')
        if len(password) < 10 or len(password) > 256 or password != getpass.getpass('비밀번호 확인: '):
            parser.error('10~256자의 동일한 비밀번호를 입력해 주세요.')
        directory = Path(os.environ.get('DATA_DIR', str(ROOT / 'data')))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'admin.hash'
        path.write_text(generate_password_hash(password))
        path.chmod(0o600)
        print('운영자 비밀번호를 설정했습니다.')
    else:
        application = create_app()
        if args.command == 'cleanup':
            print('사진 보관 기간 정리를 완료했습니다.')
        else:
            application.run(host='0.0.0.0', port=args.port, debug=False)
