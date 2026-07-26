"""WSGI entry point.

`gunicorn app:app` still works because `app` is now a package whose __init__
exposes the Flask instance, so this file is optional. It exists so the target can
be stated explicitly (`gunicorn wsgi:app`) rather than relying on a package
importing to a same-named attribute.
"""
from app import app

__all__ = ["app"]
