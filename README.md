# 🌿 Plant Tracker

A self-hosted plant tracker for a Docker homelab: a scrolling grid of photo tiles, automatic care research, photo-based plant ID, a growth timeline, and watering reminder events (ntfy-ready).

## Run it

```bash
docker compose up -d --build
```

Open `http://<host>:8080`. The first visit asks you to create the login.

Everything lives in `./data` (SQLite database + photos). Back up that folder.

## How research works

When you add a plant, every source is tried in order. For each care field, the first source that has an answer wins:

| Source | Key needed | Provides |
|---|---|---|
| iNaturalist | none | resolves common names ("snake plant") to species, reference photo |
| GBIF | none | taxonomy / family |
| Perenual | free key | watering interval, sunlight, soil, toxicity, care guides |
| Trefle | free token | light / humidity / moisture scales, temperature, pH |
| Permapeople | free key id + secret | water / light needs, hardiness, propagation |
| Wikipedia | none | description, cultivation section |
| **Ollama (local)** | your server | fills **only the fields still blank**, using everything above as context |

Without Perenual, Trefle, Permapeople or Ollama, the watering baseline falls back to the default (7 days). The other factors still apply. Add at least one of those to get real per-species baselines.

Every field shows its source, and you can override any of them. Your edits survive re-research.

## Identify by photo

**Identify** (🔍 in the header) tries Pl@ntNet first, which needs a free key with 500 IDs a day. If Pl@ntNet is not set up, or its top match is below 20%, a local Ollama vision model is used. From the results you can add the plant directly, with the photo carried over. You can also identify an existing plant from any of its timeline photos.

## Watering

```
interval = base days (researched) × pot × light × watering style × season × learned
```

Pot types, light levels, watering styles and season factors are global settings you can edit. "Learned" adjusts per plant when you tap **Was bone dry** (shorter) or **Still wet** (longer, plus a snooze).

Every 15 minutes the app creates a `water_due` event for each plant that's due, and a `water_overdue` event N days later. Events show under **Events** and at `GET /api/events`. Logging a watering resolves them.

### ntfy (when you're ready)

In Settings → Notifications, set the topic URL (e.g. `http://ntfy.lan/plants`) and this app's URL. Pending events are then pushed with a **Watered** action button that calls back into the app using the API token.

## API

Send `Authorization: Bearer <token>`; the token is shown in Settings → API.

- `GET /api/plants`, `GET /api/plants/{id}`
- `POST /api/plants/{id}/water?kind=watered|dry|wet`
- `GET /api/events?status=open|pending|sent|resolved|acked|all`
- `POST /api/events/{id}/ack`
- Interactive docs: `/api/docs`
