"""Vercel function: persistent services are mandatory, never temporary SQLite."""
from app import create_app

app = create_app({'SERVERLESS': True})
