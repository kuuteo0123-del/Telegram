# Telegram poller audit and remediation

## Findings

- `GetPeerDialogsRequest` queried all configured channels with a 4-second request timeout while observed successful latency approached 3.8 seconds. This left no network/Telegram headroom and caused most poll requests to time out.
- The poller allowed up to four concurrent requests on the same Telethon connection, increasing contention during slow periods.
- Peer resolution (`get_input_entity`) was not bounded by an explicit timeout, so rebuilding the peer list could stall the poll loop.
- Repeated poll failures reset peer state but did not explicitly hand the connection back to the reconnect supervisor. A stale connection could therefore remain connected while the poller was effectively blind.
- Production credentials and a Telegram session were present in the supplied archive, along with activity logs.

## Remediation

- Poll request timeout: **10 seconds**.
- Additional `get_messages` fetch timeout: **12 seconds**.
- Maximum concurrent poll requests: **2** (also clamped in code to no more than 2).
- Telethon automatic reconnect: **enabled**.
- Telethon connection retries: **5**.
- Peer resolution is bounded by `TELEGRAM_CHANNEL_TIMEOUT`.
- After five consecutive poll failures, the poller requests a controlled disconnect so the main Telegram supervisor can reconnect with exponential backoff.
- `TELEGRAM_LOG_ALL_INGRESS=true` is enabled in the patched private configuration for diagnosis; turn it off after confirming realtime ingress because it is verbose.

## Validation

- `python3 -m py_compile *.py`: passed.
- AST parsing of `config.py` and `main_script.py`: passed.
- No production Telegram session or live `.env` is included in the clean archive.
- The bot was not started during this audit because doing so would require the live Telegram credentials and external side effects.

## Operational follow-up

1. Revoke the exposed Telegram login session in Telegram **Settings -> Devices** and create a new session.
2. Revoke and recreate the alert bot token if it was included in the supplied `.env`.
3. Copy `.env.example` to `.env` locally and fill in fresh credentials.
4. Run one controlled production test and review `[Poll-Stat]` for timeout rate, average latency, and reconnect events.
5. After realtime ingress is confirmed, consider setting `TELEGRAM_LOG_ALL_INGRESS=false` to reduce log volume.
