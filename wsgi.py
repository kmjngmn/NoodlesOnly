"""WSGI entry point for HTTPS hosting (for example PythonAnywhere)."""
from app import create_app

application = create_app({'SESSION_COOKIE_SECURE': True})
