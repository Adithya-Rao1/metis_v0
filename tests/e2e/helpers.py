"""Shared browser steps for the end-to-end flows."""

from __future__ import annotations

import itertools

PW = "correct horse battery staple"
_n = itertools.count(1)


def new_email(prefix="user") -> str:
    return f"{prefix}{next(_n)}-{__import__('uuid').uuid4().hex[:6]}@example.org"


def sign_up(pg, stack, email, password=PW):
    """Register through the UI (terms box ticked), then follow the emailed link."""
    pg.goto(pg.base + "/auth.html")
    pg.wait_for_function("() => document.getElementById('terms-version').textContent.includes('version')")
    pg.fill("#su-email", email)
    pg.fill("#su-pass", password)
    pg.check("#accept")
    pg.click("#signup-submit")
    pg.wait_for_function(
        "() => document.getElementById('signup-msg').textContent.includes('Check your email')"
    )
    token = stack.token("verify", email)
    pg.goto(f"{pg.base}/auth.html?verify={token}")
    pg.fill("#vf-pass", password)
    pg.click("#verify-submit")
    pg.wait_for_function("() => document.getElementById('verify-msg').textContent.includes('verified')")
    return token


def log_in(pg, email, password=PW, next_page=None):
    pg.goto(pg.base + "/auth.html" + (f"?next={next_page}" if next_page else ""))
    pg.click("#tab-login")
    pg.fill("#li-email", email)
    pg.fill("#li-pass", password)
    pg.click("#login-submit")
    pg.wait_for_url(lambda u: "auth.html" not in u)


def account(pg, stack, prefix="user"):
    email = new_email(prefix)
    sign_up(pg, stack, email)
    log_in(pg, email)
    return email


def create_project(pg, name="Series A"):
    pg.goto(pg.base + "/app.html")
    pg.fill("#np-name", name)
    pg.click("#new-project button[type=submit]")
    pg.wait_for_url("**/app.html?p=*")
    pg.wait_for_selector("#pname")
    return pg.url.split("p=")[1]
