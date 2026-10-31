# The health feed (Fusion kit, schema `fusion-health/1`)

Every app behind the Bridge answers one small route in one shape. The Bridge's tiles and the Watcher read only this. No app invents its own.

The code is the Fusion kit's `fusionkit/health.py`, copied into `home_control/fusionkit/` with the rest of the kit's Python folder. Home Control was the first app to answer in this shape.

## The route

`GET /api/health`
- **Open:** no sign-in, because the Bridge and the Watcher poll it.
- **Fast:** it answers within 1 second, from state the app already holds. It never waits on a device or a cloud call.
- **Status code:** HTTP 200 whenever the app is running, even when its status is `down`, so the reader can still show why.
- **No answer within 3 seconds** means the reader shows the app as `down`, with the summary "Not answering".

## The shape

```json
{
  "schema": "fusion-health/1",
  "app": "home-control",
  "name": "Home Control",
  "version": "0.6.0",
  "status": "warn",
  "summary": "3 of 5 lights on; not answering: Sonos",
  "values": [
    {"label": "Lights", "value": "3 of 5 on", "status": "ok"},
    {"label": "Sonos", "value": "not answering", "status": "down"}
  ],
  "checked_at": "2026-10-31T21:47:00+00:00",
  "started_at": "2026-10-31T09:00:00+00:00",
  "uptime_seconds": 46020
}
```

| Field | Rule |
|---|---|
| `schema` | Always `fusion-health/1`. A later change gets a new number. |
| `app` | Stable id: lowercase letters, digits and hyphens, up to 32 characters. |
| `name` | What the tile shows. |
| `version` | The app's version. |
| `status` | `ok`: everything works. `warn`: it works, with something to look at. `down`: its main job is not working. |
| `summary` | One line, at most 120 characters, in plain words. |
| `values` | At most 6 `{label, value}` pairs, each with an optional `status`. Labels are up to 24 characters and values up to 40. |
| `checked_at` | When the feed was built (UTC, ISO 8601). |
| `started_at`, `uptime_seconds` | Optional. |

An app may add its own fields beside these, as Home Control does with V1's `connections`, `light_count`, `effects_running` and `last_event_at`. Readers ignore fields they don't know.

## Never in the feed

- **Addresses:** IP addresses (v4 or v6) and e-mail addresses.
- **Names:** account names and device names. Write "3 of 5 lights on", not a light's name.
- **Secrets,** tokens or anything from the secrets file.

`check_feed(feed, private_words=[...])` returns the problems as plain sentences, or an empty list. Each app's tests run it on the app's own feed, passing the names that app knows. The Bridge runs it too, and shows a feed that fails as `warn`, with "Health feed has a problem".

## How readers use it

- **The Bridge tile:** the dot comes from `status`, the line from `summary`, and the small text from the first two `values`.
- **The Watcher:** it records `status` and `values` over time and explains changes. Its own feed follows the same shape.
- **The Bridge's own feed** summarises the apps: "5 of 6 apps ok; Workshop warn".
