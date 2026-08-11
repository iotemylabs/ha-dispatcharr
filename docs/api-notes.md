# Dispatcharr API Notes (Phase 0 Discovery)

**Status:** Phase 0 complete — this document is the gate for Phases 1–4.
**Target instance:** `http://<HOST>:9191` (Docker container, bridge net, host port 9191)
**Dispatcharr version:** `0.29.0` (confirmed via `GET /api/core/version/` → `{"version":"0.29.0","timestamp":null}`)
**Schema source:** `GET /api/schema/?format=json` — drf-spectacular, OpenAPI 3.0.3, **227 paths**. Publicly readable (no auth).

## Method and confidence

Two sources were used, and every claim below is labelled with which one:

- **[spec]** — read from the live instance's own OpenAPI document. Authoritative for DRF viewsets.
- **[source]** — read from upstream Git at tag `v0.29.0`. Used for the `/proxy/ts/*` endpoints, which are
  plain function views that drf-spectacular cannot introspect (it emits "No response body" for all of them).

`apps/proxy/live_proxy/channel_status.py`, `apps/proxy/live_proxy/views.py`, and
`apps/accounts/authentication.py` were diffed between `main` and tag `v0.29.0` and are **byte-identical**,
so the source-derived shapes match the deployed build.

- **[live]** — verified against the running instance with an admin API key on 2026-08-11.
  See §7 for what live probing confirmed, corrected, and could not reach.

---

## 1. Upstream integration summary

Package: `custom_components/dispatcharr_sensor/` — 5 Python files, no tests, no `services.yaml`,
no `strings.json`/translations, no `options_flow`.

### Update cycle
`DispatcharrDataUpdateCoordinator` (in `__init__.py`) polls on a **fixed 30 s interval**. Each cycle:
1. `GET /proxy/ts/status` → `status_data["channels"]`. **Returns `{}` early if empty**, which wipes
   coordinator data.
2. `GET /output/epg` → re-downloads and re-parses the **entire XMLTV document every 30 s**.
3. For each active stream, fuzzy-matches `stream_name` against a channel map, then linearly scans
   all `<programme>` elements to find the one currently airing.

Returns `dict[channel_uuid, enriched_stream]`.

### Auth / token refresh
`POST /api/accounts/token/` with username+password → stores `access` only. The `refresh` token is
**discarded**. `_api_request` retries once on 401 by calling `_get_new_token()` again. There is **no
backoff**, so a persistent 401 produces a full re-login on every 30 s cycle.

### Dynamic media player entities
`DispatcharrStreamManager` (not an entity) registers a coordinator listener and diffs
`coordinator.data.keys()` against `_known_stream_ids`, adding new `DispatcharrStreamMediaPlayer`
instances. Entities are **never removed**; they go unavailable via the `available` property.
`_known_stream_ids` is only ever added to — see bug #2 below.

### EPG parsing
Two places, both in `__init__.py`: `async_populate_channel_map_from_xml()` (once at setup) and inline
inside `_async_update_data()` (every cycle). Both parse `/output/epg` with `xml.etree.ElementTree`.
Matching is `_get_channel_details_from_stream_name()`: strips a `^\w+:\s*` prefix and a trailing `HD`,
slugifies, tries exact match, then longest substring match.

### Pre-existing defects found while reading

1. **`_async_update_data` returns `{}` when no streams are active** (`__init__.py:164`). Combined with
   `async_write_ha_state()` this is benign today, but it means "no data" and "no streams" are
   indistinguishable.
2. **Ended streams are never removed from `_known_stream_ids`** (`media_player.py:57`).
   *Correction (Phase 1):* this is less severe than first written. The entity object stays alive, so
   when the same UUID streams again its `available` property flips back to True — it self-heals. The
   actual consequence is that the entity registry accumulates an unavailable `media_player` for every
   channel ever streamed, with no cleanup path. Registry hygiene, not a functional break.
3. **`resolution` / `video_codec` / `audio_codec` are always empty.** See §4.1 — the summary status
   payload does not contain them. The media player advertises three attributes that never populate.
4. **`ConfigFlow` performs no validation** (`config_flow.py:38`) — `async_create_entry` is called
   unconditionally. Any host/credentials create an entry.
5. **`const.PLATFORMS = ["sensor"]` is dead** — `__init__.py` defines its own `PLATFORMS` list.
6. `manifest.json` has no `issue_tracker`; `hacs.json` declares `"domains": ["sensor"]` but the
   integration also provides `media_player`.

---

## 2. Authentication

**[source]** `apps/accounts/authentication.py`, `dispatcharr/settings.py`

| Mode | How | Notes |
|---|---|---|
| JWT | `POST /api/accounts/token/` `{username,password}` → `{access,refresh}`; send `Authorization: Bearer <access>` | Access **30 min**, refresh **1 day** |
| API key | `X-API-Key: <key>` **or** `Authorization: ApiKey <key>` | Stateless, no expiry |

Both are registered globally in `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES`, so **API key works on
every endpoint the integration needs**, including `/proxy/ts/*`.

Key management: `POST /api/accounts/api-keys/generate/`, `POST /api/accounts/api-keys/revoke/`,
`GET /api/accounts/api-keys/`. Keys are also generatable from the UI under System → Users.

### Two constraints that shape Phase 1

**a) Everything requires an admin user.** `DEFAULT_PERMISSION_CLASSES` is `["apps.accounts.permissions.IsAdmin"]`
— a *global default*, not per-view. `IsAdmin` requires `request.user.user_level >= 10`. Every
`/proxy/ts/*` control endpoint additionally carries an explicit `@permission_classes([IsAdmin])`.
→ The config flow must surface "this account must be an admin" clearly; a valid key for a non-admin
user will authenticate and then 403 on everything.

**b) Login throttle confirmed at 3/minute.** `TokenObtainPairView` sets `throttle_classes = [LoginRateThrottle]`
and `DEFAULT_THROTTLE_RATES = {"login": "3/minute"}`. The plan's warning is accurate.
**`TokenRefreshView` is NOT throttled** — it has no `throttle_classes`. So the correct JWT strategy is:
store the refresh token (upstream currently throws it away), refresh via `/api/accounts/token/refresh/`,
and only fall back to full login when the refresh token expires (24 h) — with backoff.

Also note `network_access_allowed(request, "UI")` gates both login and refresh; Dispatcharr can be
configured to reject logins by client IP, returning **403, not 401**. Worth distinguishing in errors.

---

## 3. Endpoints for each planned service

### 3.1 `refresh_m3u` — exists, matches plan **[spec]**

| | |
|---|---|
| All accounts | `POST /api/m3u/refresh/` — "Triggers a refresh of all active M3U accounts" |
| One account | `POST /api/m3u/refresh/{account_id}/` — `account_id` is an **integer** path param |

No request body. No response body (200). Async server-side task — fire and return, as planned.
Account list for the selector: `GET /api/m3u/accounts/` → `id`, `name`, `is_active`, `status`,
`last_message`, `refresh_interval`, `cron_expression`.

### 3.2 `refresh_epg` — exists, but **`source_id` cannot be optional** **[spec]**

`POST /api/epg/import/` — "Triggers an EPG data refresh for the given source."
Body: `{"id": <int>}` — **required**, per `EPGImportRequest`. There is **no all-sources variant.**

→ To honour "all if omitted", the integration must `GET /api/epg/sources/` and fan out one POST per
source. Recommend filtering to `is_active: true`. Source fields: `id`, `name`, `source_type`, `url`,
`is_active`, `refresh_interval`, `cron_expression`, `priority`, `status`, `last_message`,
`updated_at`, `epg_data_count`.

`updated_at` on EPGSource is the natural backing value for the Phase 3
`sensor.dispatcharr_last_epg_refresh` (max across sources).

### 3.3 `refresh_all` — wrapper, no endpoint needed

### 3.4 `set_stream_priority` — exists, but **not the endpoint the plan assumed** **[spec]**

The plan implies a reorder call. `POST /api/channels/channels/{id}/reorder/` exists but is
**unrelated** — its description is "Reorder a channel by moving it after another channel… The channel
will receive the next whole number after the target channel". That is *channel numbering in the guide*,
body `{"insert_after_id": <int|null>}`. **Do not use it for failover order.**

The actual mechanism: the `Channel` serializer exposes **`streams: array[integer]` — an ordered list**.
Backend confirmation **[source]**: `next_stream` reads `channel.streams.all().order_by("channelstream__order")`,
i.e. order is held on the `ChannelStream` through-model and is what the `streams` array writes.

→ Implement as `PATCH /api/channels/channels/{id}/` with `{"streams": [id, id, …]}` (integer PK, not UUID).
Read current order from `GET /api/channels/channels/{id}/` → `streams`.
Stream names for a selector: `GET /api/channels/channels/{channel_id}/streams/`.

### 3.5 `disconnect_client` — exists **[source]**

`POST /proxy/ts/stop_client/{channel_id}` — body **`{"client_id": "<id>"}`**, required (400 `{"error":"No client_id provided"}` if absent). Undocumented in the spec; read from `views.py:1018`.

- `channel_id` here is the **channel UUID string**, not the integer PK (see §5).
- 404 if the channel or client isn't found; 200 `{"message","channel_id","client_id","locally_processed"}`.
- Client IDs come from the status payload's `clients[]` (§4.1).

Related: `POST|DELETE /proxy/ts/stop/{channel_id}` stops the **whole channel** and all its clients.

### 3.6 `force_failover` — **a real endpoint exists; the plan's fallback is unnecessary** **[source]**

`POST /proxy/ts/next_stream/{channel_id}` — "Switch to the next available stream for a channel".
No request body.

Semantics (`views.py:1053`): reads the current stream ID from Redis, loads the channel's streams in
`channelstream__order`, and rotates to the next one **with wrap-around**. Failure modes are explicit:

- `404 {"error":"No current stream found for channel"}` — channel not currently streaming.
- `404 {"error":"No alternate streams available for this channel","current_stream_id":N}` — only one stream.

→ **Recommend implementing `force_failover` as a first-class service** rather than documenting
disconnect-and-reconnect. It is more precise than `disconnect_client`: it moves the channel to the next
stream without dropping the viewer's connection. The plan said not to implement it "unless Phase 0 finds
a real endpoint" — it did.

`POST /proxy/ts/change_stream/{channel_id}` also exists ("Change stream URL for existing channel with
enhanced diagnostics") but takes a URL rather than a stream selection; not needed for the planned services.

### 3.7 `schedule_recording` — exists, but **has no `title` field** **[spec + source]**

`POST /api/channels/recordings/`. The `Recording` model is minimal:

```
channel:            integer   REQUIRED   (integer PK)
start_time:         string    REQUIRED   (ISO 8601)
end_time:           string    REQUIRED   (ISO 8601)
custom_properties:  object    nullable
id, task_id:        readOnly
```

It takes **raw times, not program IDs** — answering the plan's open question. The UI's own manual-recording
form submits exactly `{channel, start_time, end_time}`.

Title lives in `custom_properties`. The shape the backend itself writes for EPG-derived recordings
(`apps/channels/tasks.py:714`) is:

```json
{"program": {"id": 0, "tvg_id": "…", "title": "…", "sub_title": "…",
             "description": "…", "start_time": "…", "end_time": "…"}}
```

→ For a `title` service field, write `{"program": {"title": <title>}}` to match what the UI reads back.
Note `custom_properties` is also where the backend stores recording `status` and output file info at
runtime, so a blind PATCH of the whole object will clobber it — write on create only.

### 3.8 `cancel_recording` — exists **[spec]**

`DELETE /api/channels/recordings/{id}/` → 204. Its docstring confirms it closes any active DVR client
connection and removes files from disk. **This deletes recorded media, not just the schedule** — the
service description and `services.yaml` should say so plainly.

`GET /api/channels/recordings/` returns the list (backs `sensor.dispatcharr_upcoming_recordings`).

### 3.9 Bonus endpoints worth exposing later

Not in the plan, but cheap and clearly useful:

- `POST /api/channels/recordings/{id}/stop/` — stop an in-progress recording without deleting it.
- `POST /api/channels/recordings/{id}/extend/` — extend `end_time` mid-recording without interrupting.
- `GET/POST /api/channels/recurring-rules/` and `/api/channels/series-rules/` — recurring + series DVR.
- `GET /api/core/version/` — unauthenticated; ideal for config-flow reachability probing before auth.

---

## 4. Data endpoints (Phases 1, 3, 4)

### 4.1 `GET /proxy/ts/status` — summary and detail differ **[source]**

`apps/proxy/live_proxy/urls.py` routes both `status` and `status/<channel_id>` to `channel_status`.
Envelope: `{"channels": [...], "count": N}` (empty → `{"channels": [], "count": 0}`).

**Summary (`/proxy/ts/status`, all channels)** — `channel_status.py:418`:

```
channel_id (UUID str), state, url, stream_profile, owner, buffer_index,
client_count, uptime, started_at, channel_name, logo_id, m3u_profile_id,
stream_id (int), stream_name, total_bytes, avg_bitrate_kbps, avg_bitrate, healthy,
clients[]  ← capped at 10, each: client_id, user_agent, ip_address,
             connected_at, user_id, output_format, output_profile_id
```

**Detail (`/proxy/ts/status/{uuid}`) adds:** `resolution`, `video_codec`, `audio_codec`, `source_fps`,
`pixel_format`, `source_bitrate`, `sample_rate`, `audio_channels`, `audio_bitrate`, `ffmpeg_speed`,
`ffmpeg_fps`, `actual_fps`, `local_manager{healthy,connected,last_data_age}`, `diagnostics`, `total_data`.

Three consequences:

1. **The upstream integration's `resolution`/`video_codec`/`audio_codec` attributes can never populate**
   — it only ever calls the summary endpoint. Confirms defect #3.
2. **`avg_bitrate_kbps` *is* in the summary** → `sensor.dispatcharr_total_bandwidth` (Phase 3) can be
   summed from the existing single call. Note it is an *average since stream start*, not instantaneous;
   the entity name/description should not imply a live rate.
3. Restoring the codec/resolution attributes costs **one extra request per active stream per poll**.
   Recommend fetching detail only for channels that have clients, on the *slow* coordinator, and
   flagging this as a Phase 4 poll-budget decision rather than doing it on the 10–15 s cycle.

`client_count` across channels backs `sensor.dispatcharr_total_clients`. Note `clients[]` is truncated
at 10 while `client_count` is not — use `client_count` for the sensor, `clients[]` only for the
disconnect selector, and document the cap.

### 4.2 EPG without XMLTV — the `/output/epg` parsing can be deleted **[spec]**

`POST /api/epg/current-programs/` — "Get currently playing programs for specified channels or all channels".
Body: `{"channel_uuids": ["…"]}` or `{"epg_data_ids": [...]}`, both optional/nullable.
Returns `ProgramData[]`: `id`, `start_time`, `end_time`, `title`, `sub_title`, `description`, `tvg_id`.

This is a **direct replacement** for the entire XMLTV pipeline:

- The status payload's `channel_id` **is** the channel UUID (see §5), and this endpoint keys on
  `channel_uuids` — so the two join exactly, with **no name-slug fuzzy matching**.
- `channel_name` and `logo_id` are already in the status summary, so the channel map is redundant too.

→ Phase 1 should delete `async_populate_channel_map_from_xml()`, `_get_channel_details_from_stream_name()`,
and the per-cycle XMLTV re-parse. This removes the heuristic matcher, a full-document download every
30 s, and blocking `ElementTree` parsing on the event loop.

**[live] The OpenAPI schema under-documents this response.** The actual payload contains six fields the
spec's `ProgramData` does not declare:

```json
{"id": 30727, "start_time": "…", "end_time": "…", "title": "FailFactory",
 "sub_title": "Failfactory Life Hurdles", "description": "…", "tvg_id": "FailArmy.es",
 "season": 9, "episode": 13,
 "is_new": false, "is_live": false, "is_premiere": false, "is_finale": false,
 "channel_uuid": "ad17c5f0-…"}
```

→ **`season` and `episode` are returned as integers**, so `media_season`/`media_episode` are *not* lost —
and they arrive parsed, replacing upstream's `S(\d+)E(\d+)` regex over the XMLTV `episode-num` field.
`channel_uuid` is returned on each program, so the join needs no client-side bookkeeping.
`is_new`/`is_live`/`is_premiere`/`is_finale` are free additions for future attributes.

The replacement is therefore a **pure win with no behaviour loss**. `GET /api/epg/grid/` remains the
option for a future "next program" attribute.

Logos resolve via `GET /api/channels/logos/{id}/cache/` from the `logo_id` in the status payload.

---

## 5. Identifier gotcha (affects every service signature)

Dispatcharr uses **two different channel identifiers**, and the plan's `channel_id` field is ambiguous:

| Surface | Identifier |
|---|---|
| `/api/channels/channels/{id}/`, `recordings.channel`, `/api/m3u/refresh/{account_id}/` | **integer PK** |
| `/proxy/ts/*` (`status`, `stop_client`, `next_stream`, `stop`) | **channel UUID string** |

Confirmed **[source]** in `url_utils.py:49`: `get_stream_object(id)` does `get_object_or_404(Channel, uuid=id)`
and falls back to `Stream.stream_hash`.

The `Channel` object carries both (`id` and `uuid`), and the status payload carries `channel_id` (UUID)
alongside `stream_id` (int). Recommendation: have the API client hold a channel index and accept either
form in services, translating internally — rather than exposing the distinction to users writing YAML.

**This also fixes the Phase 4 "stable unique IDs" requirement**: key entities off the channel **UUID**
from the status payload (stable across renames) and use `channel_name` purely as the friendly name.

---

## 6. Summary — plan deltas

| Planned | Finding |
|---|---|
| `refresh_m3u` | ✅ As specified |
| `refresh_epg` with optional `source_id` | ⚠️ `id` is **required**; "all" must fan out over `GET /api/epg/sources/` |
| `set_stream_priority` via reorder | ⚠️ Wrong endpoint. Use `PATCH channels/{id}` `{"streams":[…]}`; `reorder` is channel *numbering*. **Untestable here — no channel has >1 stream** (§7) |
| `disconnect_client` | ✅ `POST /proxy/ts/stop_client/{uuid}` `{"client_id":…}` (undocumented; source-derived) |
| `schedule_recording` — "program IDs or raw times?" | ✅ **Raw times.** No `title` field — goes in `custom_properties.program.title` |
| `cancel_recording` | ✅ `DELETE`, 204 — **also deletes the media file** |
| `force_failover` — omit unless endpoint found | ✅ **Endpoint found.** `POST /proxy/ts/next_stream/{uuid}`. Recommend implementing. **Untestable here — always 404s with one stream** (§7) |
| Login rate limit 3/min | ✅ Confirmed. Also: refresh endpoint is **unthrottled** → prefer refresh-token flow |
| API key auth | ✅ `X-API-Key`, works on all endpoints — but **user must be admin** (`user_level >= 10`) |
| Two coordinators (Phase 4) | ✅ Supported: status is one cheap call; channels/EPG/DVR are separate and slow-moving |
| Bandwidth sensor (Phase 3) | ✅ `avg_bitrate_kbps` already in summary — but it's an **average since start**, not instantaneous |

**Nothing in the planned service list is unimplementable.** Two need a different endpoint than assumed
(`set_stream_priority`, `refresh_epg` fan-out), one gains a better primitive than planned (`force_failover`).

---

## 7. Live verification (2026-08-11)

Probed read-only with an admin API key. **No mutating endpoint was called.**

### Confirmed

- **API key auth works exactly as documented.** `X-API-Key` → 200 on `/proxy/ts/status`,
  `/api/epg/sources/`, `/api/channels/recordings/`. The key's user had `user_level: 10` and
  `is_superuser: true` — satisfying the `IsAdmin` requirement in §2a.
- **1 EPG source** (`Strong`, xmltv, active, 26 505 programs, `refresh_interval: 24`,
  `updated_at: 2026-08-10T20:47:23Z`). The `refresh_epg` fan-out is a single call here.
- **7 M3U accounts** (ids 1,2,3,5,6,7,10). Account `1 / custom` is in `status: error` — the
  `refresh_m3u`-all path will hit it, so per-account failures must not abort the whole batch.
- **119 channels.** `id` (PK) and `uuid` coexist on every channel, confirming §5.

### Corrected

- **`current-programs` returns `season`/`episode`** — see §4.2. My earlier note that episode data
  would be lost was wrong; the OpenAPI schema simply under-declares the response.

### Blocked — not exercisable on this instance

- **No channel has more than one stream.** Distribution across all 119 channels is `{1: 119}`.
  Consequences:
  - `force_failover` will always return `404 {"error":"No alternate streams available…"}` here.
  - `set_stream_priority` has nothing to reorder.

  Both are still worth implementing (the endpoints are real and the semantics are known from source),
  but **neither can be integration-tested against this instance** until a channel is given a second
  stream. Recommend adding one channel with two streams purely as a test fixture.
- **No streams were active at probe time** (`{"channels": [], "count": 0}`), so the populated summary
  payload in §4.1 remains source-derived. `avg_bitrate_kbps` presence in practice is still unverified.
- **No recordings and no recurring/series rules exist** (`[]`, `{"rules":[]}`), so `cancel_recording`
  and the upcoming-recordings sensor have no live data. Verifying `schedule_recording` requires
  *creating* a recording — a mutating call I have not made.

### Two live behaviours the integration must handle

1. **`GET /proxy/ts/status/{uuid}` on an idle channel returns `404 {"error": "Channel <uuid> not found"}`.**
   This means "not currently streaming", **not** "no such channel". Surfacing that message verbatim to
   users would be actively misleading — the client must translate it.
2. **`GET /api/accounts/users/me/` returns the raw `api_key` in plaintext.** If that response is ever
   logged at debug level the credential leaks into the HA log. Do not log this response body, and
   redact `api_key` if the endpoint is used for config-flow validation.

### EPG linkage (affects the §4.2 rewrite)

- `epg_data_id` set on **87 / 119** channels; `tvg_id` set on only **25 / 119**.
- → Join on `channel_uuid` / `epg_data_id`, **never on `tvg_id`** — it is empty for most channels.
- ~32 channels will legitimately have no program data; the media player must treat that as normal
  rather than as an error.

## Reference artifacts

- Full OpenAPI document: `<scratchpad>/openapi.json` (723 KB) — not committed; re-fetch with
  `curl -s http://<HOST>:9191/api/schema/?format=json`
- Path/method inventory for all 227 endpoints was generated from it; the subset relevant to this fork
  is documented above.
