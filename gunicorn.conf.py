"""Gunicorn-config. Eén worker (de downloadwachtrij leeft in het proces), veel threads
omdat browserdownloads een thread bezet houden zolang de stream loopt."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '2233')}"
workers = 1
worker_class = 'gthread'
threads = int(os.environ.get('THREADS', '16'))
timeout = 0            # lange streams niet afbreken
graceful_timeout = 10
keepalive = 5
accesslog = '-' if os.environ.get('ACCESS_LOG', 'false').lower() == 'true' else None
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info').lower()
forwarded_allow_ips = '*'
