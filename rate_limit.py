"""Shared Flask-Limiter instance.

This lives in its own module rather than inside app.py or routes.py to
avoid a circular import: app.py needs it to call limiter.init_app(app),
and routes.py needs it to decorate individual routes with tighter limits
than the blanket default -- if app.py imported routes.py (it already
does, to register the blueprint) and routes.py also imported the limiter
from app.py, neither module could finish importing first.

Storage backend: this uses Flask-Limiter's default in-memory store, which
is fine for the single-process deployment this app currently runs as
(Render, one dyno). It resets on every restart/deploy (acceptable -- these
are abuse guards, not billing records) but does NOT share counts across
multiple worker processes or dynos. If this app is ever scaled to more
than one gunicorn worker or dyno, each process would enforce these limits
independently, so a client could get up to (limit x worker count) requests
through before *any* single worker starts rejecting -- looser than the
numbers below suggest, but each worker still fails safely well short of
letting the app fall over. Moving to a shared backend (Redis is
Flask-Limiter's other first-class option, e.g. storage_uri="redis://...")
would close that gap; not done here since this app has no Redis
(or any other shared cache) in its deployment yet, and adding one just for
this would be a bigger infrastructure change than "add rate limiting"
calls for.
"""

from flask import session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def rate_limit_key() -> str:
    """Key rate limits per signed-in user where there is one (so one
    tenant's heavy use can't eat into another's), falling back to the
    request's IP address for anonymous requests -- login/signup/forgot-
    password all happen before a session exists to key off of."""

    return session.get("user_id") or get_remote_address()


limiter = Limiter(
    key_func=rate_limit_key,
    # Applies to every route in the app unless a specific @limiter.limit(...)
    # decorator tightens it further (that decorator adds an *additional*
    # constraint, it doesn't replace this one) -- a blanket failsafe so a
    # route nobody thought to specifically guard still can't be hammered
    # into the ground.
    default_limits=["200 per hour", "60 per minute"],
)
