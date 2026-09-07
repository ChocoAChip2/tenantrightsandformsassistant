# Data encryption at rest

Message bodies and conversation titles are encrypted by the application
before they are written to Supabase, and decrypted on the way back out.
Implementation: [`crypto_service.py`](../crypto_service.py), wired into
[`supabase_service.py`](../supabase_service.py).

## What this protects, and what it does not

**It protects against anyone who reaches the stored data without reaching
the app process.** A leaked or stolen database dump, a compromised
Supabase project, an RLS policy that gets misconfigured later, a future
collaborator or support engineer with SQL console access — all of them see
this:

```
enc:v1:k1:eQU70uKy6eMZ0HUU8wocDgk4Wo40vVx/AMcLCirvR2L1HGQRiYgb3sgv/B9L...
```

instead of a tenant's account of their eviction or their landlord dispute.
That is a real boundary and it is the one most likely to be crossed.

**It does not protect against a compromised app server.** The key lives in
that process's environment, so anything that can run code there can
decrypt. This is a deliberate limit, not an oversight — the alternative
isn't available to this product:

> True end-to-end encryption, where only the user's own password unlocks
> their data and the server genuinely cannot read it, is **incompatible
> with what this app does**. Gemini has to read the conversation
> server-side to answer it, and the RA-81 PDF is filled from the same
> text. Encrypting so the server can't read it means the assistant can't
> read it either, and there is no product left. It would also mean a
> forgotten password permanently destroys every conversation, because
> nothing would remain that could re-derive the key.

So the accurate description is **"encrypted at rest under a key the
application holds"** — not "only the user can ever read this". Don't
describe it to users as the latter.

Not encrypted, on purpose: `user_id`, `conversation_id`, `role`,
timestamps and `archived_at`. Those are what RLS filters and the app
sorts and joins on; encrypting them would break every query while
protecting metadata that the row's existence already reveals.

## Configuration

Both are optional. **With no key configured, encryption is off and every
function is a pass-through** — so this can be deployed before any key
exists without changing behavior, and switched on later without a
migration.

| Variable | Meaning |
| --- | --- |
| `DATA_ENCRYPTION_KEYS` | `id:base64key` pairs, comma-separated. Every key that any stored row might still be using. |
| `DATA_ENCRYPTION_ACTIVE_KEY_ID` | Which of those encrypts *new* data. Optional when exactly one key is configured. |

Generate a key:

```bash
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
```

Then in Render:

```
DATA_ENCRYPTION_KEYS=k1:<that base64 value>
DATA_ENCRYPTION_ACTIVE_KEY_ID=k1
```

**Losing every key means losing every encrypted row.** There is no
recovery path and that is the nature of the thing — keep a copy somewhere
that is not only Render's dashboard.

## Designed to be changed later

Encryption schemes age, so nothing here assumes today's choice is
permanent. Every value carries its own envelope describing how to read it:

```
enc:v<algorithm version>:<key id>:<base64 payload>
```

Because the instructions travel with the data, old rows never have to be
migrated before something new can be adopted.

### Rotating the key

1. Generate a second key.
2. Add it **alongside** the current one and make it active:
   ```
   DATA_ENCRYPTION_KEYS=k1:<old>,k2:<new>
   DATA_ENCRYPTION_ACTIVE_KEY_ID=k2
   ```
3. Deploy. New writes use `k2`; everything under `k1` still decrypts.
4. Rows re-encrypt themselves under `k2` as normal use reads them (see
   below). Leave `k1` listed until nothing uses it — removing it early
   raises a loud `DecryptionError` naming the missing key rather than
   silently losing data.

### Changing the algorithm

1. Add a class to `crypto_service.py` with `encrypt`/`decrypt` and a new
   `version`.
2. Register it in `_ALGORITHMS`.
3. Point `CURRENT_VERSION` at it.

Version 1 rows keep decrypting through the version 1 class forever. Never
delete an old algorithm while any row might still use it.

This is also the path for improvements that change the ciphertext shape —
for example binding each value to its `user_id` as AES-GCM additional
authenticated data, so a ciphertext cannot be copied from one user's row
into another's. That is worth doing and is deliberately left as a v2
rather than retrofitted onto v1, precisely because the versioned envelope
makes adding it a non-event.

### The background migration

`crypto_service.needs_rewrap()` reports whether a value is behind the
current scheme — plaintext that could now be encrypted, or ciphertext
under a retired key or older algorithm. `supabase_service` checks it on
every read and quietly rewrites stale rows under the current scheme.

So a rotation or an algorithm change **migrates itself over normal use**.
There is no batch job, no downtime, and no single risky pass over the
whole table.

Two deliberate limits:

- **Capped per read** (`SupabaseService.REWRAP_BATCH_LIMIT`, 25). A long
  conversation opened for the first time after encryption is enabled would
  otherwise fire one `UPDATE` per message inside a page render. The
  upgrade converges over a few visits instead of making any one of them
  slow.
- **Best-effort.** A failed rewrite is logged and dropped. The row is
  still perfectly readable as it stands, and the next read retries. A
  cleanup task must never be able to break the page it runs inside.

Rows nobody ever opens again stay in their old form indefinitely. That is
acceptable for a rotation (the old key still opens them) but means a key
cannot be retired on read-traffic alone — a deliberate backfill pass would
be needed for that, and is not built.
