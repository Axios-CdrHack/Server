# AXIOS Django API

API-compatible Django replacement for the previous Express server.

```bash
cp .env.example .env
source ../.venv/bin/activate
python manage.py migrate
python manage.py runserver
```

`manage.py runserver` binds to `0.0.0.0:8001`.
The frontend API client uses `http://localhost:8001`.

Useful checks:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/axios_pycache python3 manage.py test
python3 manage.py check
```
# Server
