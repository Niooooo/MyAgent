# Service configuration

`config/service.json` must keep the existing endpoint and backoff, but the retry
attempt count must be a JSON integer, retry jitter must be enabled, and production
logging must use the `info` level.
