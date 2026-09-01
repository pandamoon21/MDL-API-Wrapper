"""Vercel serverless entrypoint for mdlaw.

Vercel Python functions call `app` (a WSGI/ASGI callable) per request.
FastAPI's ASGI app is exposed via `asgi_app`; Vercel wraps it.
See https://vercel.com/docs/functions/serverless-functions/runtimes/python
"""
from mdlaw import app as asgi_app

# Vercel expects a module-level `app` WSGI/ASGI callable.
app = asgi_app
