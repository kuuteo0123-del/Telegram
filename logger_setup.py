# AutoBot — production-ready v2

This revision keeps the architecture you asked for and removes the original hot path issues:

- Telegram ingress is decoupled from the processing graph
- SQLite writes are batched instead of commit-per-message
- worker loop handles code extraction and routing
- domain queue prevents browser saturation
- config is environment-driven and production-safe

## Quick start

1. Copy `.env.example` to `.env` and fill your Telegram credentials.
2. Install dependencies:
   python -m pip install -r requirements.txt
3. Run:
   python main_script.py

## Notes

- The project is intentionally modular and easy to extend.
- `durable_inbox_v2.py` is the queue layer to keep under load.
- `main_script.py` is the entrypoint for Telegram + worker orchestration.
- `config.py` centralizes environment and runtime tuning.

## Production tuning highlights

- `CHANNEL_POLL_INTERVAL`: keep low but stable under burst conditions.
- `MAX_INBOX_ATTEMPTS`: cap replay storms and prevent stale message loops.
- `MESSAGE_WORKERS`: increase only if extraction/validation is the bottleneck.
- `MAX_CONCURRENT_PROCESSING`: set according to your CPU and browser capacity.
- `ACTIVE_DOMAINS`: restrict to the domains you actually use.

## Security

Do not commit `.env` or `.session` files.

# README.md
