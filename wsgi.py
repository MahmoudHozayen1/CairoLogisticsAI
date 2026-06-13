"""WSGI entry point for production servers (gunicorn/uWSGI).

Example::

    gunicorn "wsgi:app" --bind 0.0.0.0:8000
"""
import os
from app import create_app

app = create_app(os.environ.get("FLASK_CONFIG", "production"))
