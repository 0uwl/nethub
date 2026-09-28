"""Response headers and the confirmation check every destructive form uses
(PLAN.md WS-11).

The pages carry no JavaScript at all, so the Content-Security-Policy can
refuse every script outright. That is the point of it: the login and
approval forms are where admins type AAA passwords, and an injected script
there would read them.
"""
from flask import flash, request

#: `script-src 'none'`: no page has a script, inline or external.
#: `img-src 'self' data:` is the one widening of `default-src 'self'`: Pico
#: draws its checkbox ticks, select chevrons and `<details>` markers as
#: `data:` SVG images in the stylesheet, and without it those vanish. An image
#: cannot run code, so this costs the policy nothing that matters.
#: `style-src` falls back to `'self'`, which refuses `style=` attributes and
#: `<style>` blocks: styling lives in the two files under static/css.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "script-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'"
)

SECURITY_HEADERS = {
    'Content-Security-Policy': CONTENT_SECURITY_POLICY,
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin',
}


def set_security_headers(response):
    """`after_request` hook. Sets rather than defaults, so a route cannot
    loosen them by accident."""
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def confirmed(action):
    """Did the POST carry its ticked confirmation box? Flash a refusal if not.

    The six destructive forms (approve, retry, cancel, delete an artifact,
    delete a host-key pin, disable a user) each carry a required checkbox
    named `confirm` whose label states the consequence. `required` is a
    browser convenience; this is the check. A route calls it before doing
    anything, so an unticked POST changes nothing and spends no credential.
    """
    if request.form.get('confirm') == 'yes':
        return True
    flash(f'Tick the box to confirm {action}; nothing was changed.')
    return False
