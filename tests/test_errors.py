"""Checks for the error handlers in nethub/__init__.py.

404 and 500 render Werkzeug's own default page's replacement templates with
no interesting logic to test. 413 is the one WS-5.6 added: it has to state
the actual limit, since Werkzeug's bare default page doesn't say why the
request failed or what to do about it.
"""


def test_413_states_the_configured_limit(client, app):
    app.config['MAX_CONTENT_LENGTH'] = 10
    response = client.post('/login', data={'username': 'x' * 100, 'password': 'y'})
    assert response.status_code == 413
    assert b'10 Bytes' in response.data
    assert b'too large' in response.data.lower()
