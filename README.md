# Queueflow

A small full-stack queue management app. Python (Flask + SQLite) backend, vanilla JS frontend.

## Live Demo
https://queueflow-5rk3.onrender.com
The free-tier server sleeps when idle, so the first load may take up to a minute.

## Run
```bash
pip install -r requirements.txt
python app.py          # http://localhost:5000
```
Optional env vars: `SECRET_KEY`, `DB_PATH`, `PORT`.

## Features
Manager sign-up/login, create named queues, add people (token auto-generated, e.g. `C007`), move tokens up/down,
serve the top token with one click, complete or cancel tokens, analytics dashboard (average wait, average service time,
queue-length trend, recent wait times, served/cancelled counts).

## API (all under `/api`, session-cookie auth)
| Method | Path | Purpose |
|---|---|---|
| POST | `/register`, `/login`, `/logout` · GET `/me` | auth |
| GET/POST | `/queues` | list / create queue |
| GET/DELETE | `/queues/<id>` | queue detail (waiting + serving) / delete |
| POST | `/queues/<id>/tokens` | add person `{name}` |
| POST | `/queues/<id>/serve` | serve top token |
| POST | `/tokens/<id>/move` | `{dir:"up"|"down"}` |
| POST | `/tokens/<id>/complete` · `/cancel` | finish / cancel |
| GET | `/queues/<id>/analytics` | dashboard data |

## Design notes
- Token order is a `position` column; moving swaps positions with the neighbouring waiting token.
- Every change to the waiting count is written to an `events` table, which powers the queue-length trend chart.
- Wait time = `started_at - created_at` (set when the token is served).
- Passwords hashed with Werkzeug; every queue/token route checks the queue belongs to the logged-in manager.
