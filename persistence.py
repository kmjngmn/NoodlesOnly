"""Persistent database and private photo storage for serverless hosting."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


class PostgresConnection:
    """The small SQLite connection surface used by this application's fixed SQL."""

    WRITE_LOCK = 731944028

    def __init__(self, dsn):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row

        options = conninfo_to_dict(dsn)
        options['sslmode'] = 'verify-full'
        options.setdefault('sslrootcert', os.environ.get('SSL_CERT_FILE') or 'system')
        options.setdefault('connect_timeout', 10)
        self.connection = psycopg.connect(
            **options, autocommit=False, prepare_threshold=None, row_factory=dict_row)
        self._transaction_ready = False

    def _prepare_transaction(self):
        if not getattr(self, '_transaction_ready', False):
            # Transaction poolers may assign a different server after each commit.
            self.connection.execute('SET LOCAL search_path = study_app')
            self.connection.execute("SET LOCAL statement_timeout = '20s'")
            self._transaction_ready = True

    def execute(self, sql, args=()):
        self._prepare_transaction()
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            return self.connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.WRITE_LOCK,))
        # These are app-owned queries, not a general SQL parser; values remain bound parameters.
        return self.connection.execute(sql.replace('?', '%s'), args)

    def executemany(self, sql, rows):
        self._prepare_transaction()
        with self.connection.cursor() as cursor:
            cursor.executemany(sql.replace('?', '%s'), rows)

    def __enter__(self):
        # ponytail: one transaction lock fits a 10-person study; use narrower locks if scaled.
        self.execute('BEGIN IMMEDIATE')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def commit(self):
        self.connection.commit()
        self._transaction_ready = False

    def rollback(self):
        self.connection.rollback()
        self._transaction_ready = False

    def close(self):
        self.connection.close()

    def setup_schema(self):
        with self:
            exists = self.connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname='study_app'").fetchone()
            if not exists:
                self.connection.execute('CREATE SCHEMA study_app')
            schema = Path(__file__).with_name('schema-postgres.sql').read_text()
            self.connection.execute(schema.replace('CREATE SCHEMA IF NOT EXISTS study_app;', ''))


class StorageError(RuntimeError):
    """A storage operation failed; messages never contain credentials or remote response bodies."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the storage credential to a redirect destination.
        raise urllib.error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


class SupabasePhotoStore:
    MAX_PHOTO_BYTES = 10 * 1024 * 1024

    def __init__(self, url, service_key, bucket='proofs'):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname
                or not re.fullmatch(r'[a-z0-9-]+\.supabase\.co', parsed.hostname)
                or parsed.netloc != parsed.hostname or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise ValueError('SUPABASE_URL must be the HTTPS project URL without a path.')
        if not isinstance(service_key, str) or not service_key or any(c in service_key for c in '\r\n'):
            raise ValueError('A Supabase server credential is required.')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', bucket):
            raise ValueError('Invalid photo bucket name.')
        self.origin = url.rstrip('/')
        self.bucket = bucket
        self.key = service_key
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _name(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{48}\.jpg', name):
            raise ValueError('Invalid photo name.')
        return name

    def _request(self, method, path, payload=None, content_type=None):
        headers = {'Authorization': f'Bearer {self.key}', 'apikey': self.key}
        if content_type:
            headers['Content-Type'] = content_type
        request = urllib.request.Request(self.origin + '/storage/v1/' + path,
                                         data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                result = response.read(self.MAX_PHOTO_BYTES + 1)
                if len(result) > self.MAX_PHOTO_BYTES:
                    raise StorageError('사진 저장소 응답이 너무 큽니다.')
                return result
        except urllib.error.HTTPError as error:
            missing = error.code == 404
            error.close()
            if missing and method == 'DELETE':
                return b''
            if missing and method == 'GET':
                raise FileNotFoundError('사진을 찾을 수 없습니다.') from None
            raise StorageError('사진 저장소 요청을 완료하지 못했습니다.') from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise StorageError('사진 저장소에 연결하지 못했습니다.') from None

    def put(self, name, blob):
        name = self._name(name)
        if not isinstance(blob, bytes) or not blob or len(blob) > self.MAX_PHOTO_BYTES:
            raise ValueError('Invalid photo bytes.')
        self._request('POST', f'object/{self.bucket}/{name}', blob, 'image/jpeg')

    def get(self, name):
        name = self._name(name)
        return self._request('GET', f'object/authenticated/{self.bucket}/{name}')

    def delete(self, name):
        name = self._name(name)
        payload = json.dumps({'prefixes': [name]}).encode()
        self._request('DELETE', f'object/{self.bucket}', payload, 'application/json')
