"""Production entry point (used by the Docker image: `gunicorn wsgi:app`).

`run.py` is for local development with Flask's built-in server. This module differs in one way that
matters: the app's development config sets DEBUG=True, which `run.py` switches off again via
`app.run(debug=False)`. A WSGI server never calls `app.run()`, so DEBUG is switched off here instead -
otherwise unhandled errors would escape as raw server errors rather than the app's own 500 page.
"""
from app import create_app

app = create_app()
app.debug = False
