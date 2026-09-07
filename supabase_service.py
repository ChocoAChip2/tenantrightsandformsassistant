"""Supabase client wrapper used by the Flask routes.

This file centralizes all auth/data communication with Supabase so routes.py
can focus on request handling instead of client setup details.
"""

import logging

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from supabase import Client, create_client

import crypto_service
from config import Settings

logger = logging.getLogger(__name__)


@dataclass
class SupabaseService:
    """Small service layer that owns the Supabase client and auth actions."""

    client: Client | None
    initialization_error: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "SupabaseService":
        """Create the shared Supabase client from values loaded in config.py."""

        # Return a readable startup error when app.py has not been given the
        # environment variables needed to talk to Supabase.
        if not settings.supabase_url or not settings.supabase_key:
            return cls(client=None, initialization_error="Supabase keys are missing.")

        try:
            # Build the SDK client once and reuse it for every request.
            client = create_client(settings.supabase_url, settings.supabase_key)
            return cls(client=client)
        except Exception as exc:
            return cls(client=None, initialization_error=f"Failed to create Supabase client: {exc}")

    def is_ready(self) -> bool:
        """Tell app.py whether auth routes can safely use the Supabase client."""

        return self.client is not None

    def sign_up(self, email: str, password: str) -> bool:
        """Create a new Supabase account for the signup route.

        Returns False instead of raising when the email already has an
        account, so routes.py can send the visitor to login instead of a
        false "check your email" success message. Supabase deliberately does
        not make this easy to detect: to avoid leaking which emails are
        registered, sign_up() for an existing *confirmed* email returns a
        look-alike success response with an empty `identities` list rather
        than an error, when email confirmation is required. If email
        confirmation is turned off in the dashboard, it instead raises with
        "already registered" in the message. Both are handled here so this
        works regardless of that project setting.
        """

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")

        try:
            response = self.client.auth.sign_up({"email": email, "password": password})
        except Exception as exc:
            if "already registered" in str(exc).lower() or "already exists" in str(exc).lower():
                return False
            raise

        identities = getattr(response.user, "identities", None) if response.user else None
        if identities is not None and len(identities) == 0:
            return False

        return True

    def sign_in(self, email: str, password: str):
        """Authenticate an existing Supabase user for the login route."""

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")
        return self.client.auth.sign_in_with_password({"email": email, "password": password})

    def verify_user_jwt(self, access_token: str):
        """Validate an incoming Supabase JWT and return the authenticated user."""

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")
        return self.client.auth.get_user(access_token)

    def build_user_scoped_client(self, access_token: str) -> Client:
        """Create a request-scoped Supabase client that carries the user's JWT.

        Using the user's JWT (instead of a service-role key) ensures RLS policies
        are enforced for every select/insert/update call.
        """

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")

        auth_client = create_client(str(self.client.supabase_url), self.client.supabase_key)
        auth_client.postgrest.auth(access_token)
        return auth_client

    def create_conversation(self, user_client: Client, user_id: str, title: str) -> str:
        """Create a new conversation row for the authenticated user and return its id."""

        response = (
            user_client
            .table("conversations")
            .insert({"user_id": user_id, "title": crypto_service.encrypt(title)})
            .execute()
        )
        return response.data[0]["id"]

    def delete_empty_conversations(self, user_client: Client, user_id: str) -> None:
        """Delete any of this user's conversations that were created but
        never actually used (zero messages).

        Called right before creating a new conversation (see routes.py's
        create_conversation) as the server-side backstop for the sidebar's
        disable-on-submit spam guard: that JS closes the common
        double-click race, but doesn't stop two separate tabs (or a
        scripted client) from each creating an empty conversation, so
        without this a user who does that ends up with a pile of "New
        conversation" rows they never sent a single message in. This only
        ever touches conversations with zero messages -- anything with
        even one message, however old or apparently abandoned, is left
        alone.

        There's no single postgrest call for "delete rows with no
        matching child row", so this is three round trips: the user's
        conversation ids, which of those ids appear in messages, and a
        delete of the ones that don't. Fine at the scale one tenant's
        conversation list runs at (same tradeoff already made by
        fetch_all_conversations_with_messages above); not something to
        reach for at a larger scale without a proper SQL view.
        """

        conversations = (
            user_client.table("conversations").select("id").eq("user_id", user_id).execute()
        ).data or []
        if not conversations:
            return

        conversation_ids = [row["id"] for row in conversations]

        messages = (
            user_client
            .table("messages")
            .select("conversation_id")
            .in_("conversation_id", conversation_ids)
            .execute()
        ).data or []
        ids_with_messages = {row["conversation_id"] for row in messages}

        empty_ids = [cid for cid in conversation_ids if cid not in ids_with_messages]
        if not empty_ids:
            return

        user_client.table("conversations").delete().in_("id", empty_ids).execute()

    def ensure_conversation_for_user(self, user_client: Client, conversation_id: str, user_id: str) -> None:
        """Ensure the target conversation exists and belongs to the authenticated user."""

        response = (
            user_client
            .table("conversations")
            .select("id")
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        if not response.data:
            raise ValueError("Conversation not found for this user.")

    def fetch_messages_for_conversation(self, user_client: Client, conversation_id: str) -> list[dict]:
        """Fetch full message history for a conversation, ordered oldest->newest.

        Also opportunistically re-encrypts bodies that are behind the
        current scheme (plaintext from before encryption was enabled, or
        sealed under a retired key/older algorithm), capped per call --
        see _rewrap_messages.
        """

        response = (
            user_client
            .table("messages")
            .select("id,role,content,created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .execute()
        )
        rows = response.data or []

        stale_ids = [
            row["id"] for row in rows
            if "id" in row and crypto_service.needs_rewrap(row.get("content"))
        ]
        for message in rows:
            message["content"] = crypto_service.decrypt(message.get("content"))
        if stale_ids:
            self._rewrap_messages(user_client, stale_ids, rows)

        for message in rows:
            message.pop("id", None)
        return rows

    #: Most message bodies re-encrypted during a single read. A long
    #: conversation opened for the first time after encryption is switched
    #: on would otherwise fire one UPDATE per message inside a page
    #: render. Capping it means the upgrade converges over a few visits
    #: instead of making any one of them slow.
    REWRAP_BATCH_LIMIT = 25

    def _rewrap_messages(self, user_client: Client, stale_ids: list[str], rows: list[dict]) -> None:
        """Re-encrypt message bodies under the current scheme, best-effort.

        `rows` already hold decrypted content by this point, so this
        re-seals from plaintext rather than decrypting a second time. A
        failure is logged and dropped: the rows are still perfectly
        readable as they are, and the next read retries.
        """
        by_id = {row["id"]: row for row in rows if "id" in row}
        for message_id in stale_ids[: self.REWRAP_BATCH_LIMIT]:
            row = by_id.get(message_id)
            if row is None:
                continue
            try:
                user_client.table("messages").update(
                    {"content": crypto_service.encrypt(row["content"])}
                ).eq("id", message_id).execute()
            except Exception:
                logger.exception("Failed to re-encrypt a message body; leaving it as-is.")
                return

    def insert_message(self, user_client: Client, message: dict) -> None:
        """Insert a single message row under RLS.

        The message body is wrapped by crypto_service on the way in (a
        no-op when no key is configured). Copied rather than mutated in
        place so the caller's dict -- which routes.py also uses to build
        the reply it sends back to the browser -- still holds plaintext.
        """

        row = dict(message)
        row["content"] = crypto_service.encrypt(row.get("content"))
        user_client.table("messages").insert(row).execute()

    def fetch_all_conversations_with_messages(self, user_client: Client) -> list[dict]:
        """Fetch every conversation for the authenticated user with its full history.

        Used by the "download my chat history" settings feature -- one extra
        query per conversation, which is fine at the scale a single tenant's
        chat history runs at and keeps this on the same RLS-scoped client as
        every other read in this file. Includes archived conversations too
        (list_conversations defaults to active-only, so both are fetched
        explicitly) -- a user archiving something shouldn't make it silently
        disappear from their own downloadable record of it.
        """

        conversations = self.list_conversations(user_client, archived=False) + self.list_conversations(
            user_client, archived=True
        )
        for conversation in conversations:
            conversation["messages"] = self.fetch_messages_for_conversation(user_client, conversation["id"])
        return conversations

    def send_password_reset_email(self, email: str, redirect_to: str | None = None) -> None:
        """Ask Supabase to email a password-reset link to this address.

        Supabase's underlying `recover` endpoint returns success regardless
        of whether an account exists for this email (this is deliberate on
        Supabase's part, to avoid letting an attacker use this endpoint to
        discover which emails are registered) -- so routes.py's
        forgot_password always shows the same generic confirmation message
        no matter what happens here, and never reports back whether an
        account actually existed.

        redirect_to should point at this app's /reset-password route (with
        _external=True so it's a full URL) -- Supabase appends the recovery
        access/refresh tokens to that URL as a fragment when the visitor
        clicks the emailed link. It must also be added to this Supabase
        project's Auth -> URL Configuration -> Redirect URLs allowlist, or
        Supabase will silently fall back to the project's default Site URL
        instead of sending the visitor back to this app.
        """

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")

        options: dict[str, str] = {}
        if redirect_to:
            options["redirect_to"] = redirect_to
        self.client.auth.reset_password_email(email, options)

    def update_account(
        self,
        access_token: str,
        refresh_token: str,
        email: str | None = None,
        password: str | None = None,
    ):
        """Change the authenticated user's email and/or password.

        This goes through the Supabase auth (GoTrue) client rather than
        postgrest, so it needs a real auth session established via
        set_session(access_token, refresh_token) -- build_user_scoped_client's
        postgrest-only auth wiring doesn't give the auth client that session.
        A change to email is not applied until the user confirms it from a
        link Supabase emails to the new address (standard Supabase behavior);
        a password change takes effect immediately.
        """

        if not self.client:
            raise RuntimeError("Supabase is not configured yet.")

        attributes = {}
        if email:
            attributes["email"] = email
        if password:
            attributes["password"] = password
        if not attributes:
            raise ValueError("Provide a new email and/or password.")

        account_client = create_client(str(self.client.supabase_url), self.client.supabase_key)
        account_client.auth.set_session(access_token, refresh_token)
        return account_client.auth.update_user(attributes)

    def list_conversations(self, user_client: Client, archived: bool = False) -> list[dict]:
        """Return the user's conversations sorted by most recent activity.

        archived=False (the default) returns active conversations -- what
        the sidebar shows. archived=True returns only archived ones, for
        the collapsible "Archived" section. A conversation is one or the
        other, never both, so callers never need to de-duplicate.
        """

        query = (
            user_client
            .table("conversations")
            .select("id,title,created_at,updated_at,archived_at")
            .order("updated_at", desc=True)
        )
        query = query.not_.is_("archived_at", "null") if archived else query.is_("archived_at", "null")
        response = query.execute()
        conversations = response.data or []
        for conversation in conversations:
            stored_title = conversation.get("title")
            conversation["title"] = crypto_service.decrypt(stored_title)
            # Opportunistic upgrade: a title still in plaintext, or sealed
            # under a retired key or an older algorithm, gets rewritten
            # under the current scheme as it is read. That is what makes a
            # rotation or an algorithm change migrate itself over normal
            # use rather than needing one big re-encryption pass.
            if crypto_service.needs_rewrap(stored_title):
                self._rewrap_conversation_title(user_client, conversation["id"], conversation["title"])
        return conversations

    def _rewrap_conversation_title(self, user_client: Client, conversation_id: str, title: str) -> None:
        """Re-encrypt one conversation title under the current scheme.

        Strictly best-effort. This runs inside a plain page render, so a
        failure here must never break showing the sidebar -- the row stays
        readable exactly as it was and will simply be retried next time.
        """
        try:
            user_client.table("conversations").update(
                {"title": crypto_service.encrypt(title)}
            ).eq("id", conversation_id).execute()
        except Exception:
            logger.exception("Failed to re-encrypt a conversation title; leaving it as-is.")

    def set_conversation_archived(
        self, user_client: Client, conversation_id: str, user_id: str, archived: bool
    ) -> None:
        """Archive (soft-hide) or unarchive a conversation.

        This never touches messages -- archiving just sets archived_at so
        list_conversations stops surfacing it in the main sidebar list,
        while the conversation and its history stay fully intact and
        reachable (e.g. from the Archived section, or a direct link).
        """

        archived_at = datetime.now(timezone.utc).isoformat() if archived else None
        response = (
            user_client
            .table("conversations")
            .update({"archived_at": archived_at})
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not response.data:
            raise ValueError("Conversation not found for this user.")

    def rename_conversation(
        self, user_client: Client, conversation_id: str, user_id: str, title: str
    ) -> None:
        """Set a conversation's display title.

        routes.py does the trimming/length/emptiness validation before this
        is ever called (same division of labor as create_conversation's
        title default) -- this just writes whatever title it's given,
        scoped to the owning user the same way archive/delete are.
        """

        response = (
            user_client
            .table("conversations")
            .update({"title": crypto_service.encrypt(title)})
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not response.data:
            raise ValueError("Conversation not found for this user.")

    def request_account_deletion(
        self, user_client: Client, user_id: str, grace_period_days: int
    ) -> str:
        """Schedule this account for deletion after a grace period.

        Writes one row into account_deletion_requests and returns the
        purge_after timestamp (ISO 8601) so the caller can tell the user the
        exact date. Nothing is deleted here and the account keeps working
        normally -- see supabase/migrations/20260907_account_deletion_requests.sql
        for the pg_cron job that does the actual deleting once the deadline
        passes, and why it lives in the database rather than in this app.

        Upserts rather than inserts: user_id is the table's primary key, so
        asking twice moves the deadline instead of erroring on a duplicate
        key. That's also the behavior you'd want -- the second request is
        the one the user just saw a date for.
        """

        purge_after = datetime.now(timezone.utc) + timedelta(days=grace_period_days)
        purge_after_iso = purge_after.isoformat()

        user_client.table("account_deletion_requests").upsert(
            {
                "user_id": user_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "purge_after": purge_after_iso,
            }
        ).execute()

        return purge_after_iso

    def cancel_account_deletion(self, user_client: Client, user_id: str) -> bool:
        """Cancel a pending deletion. Returns whether there was one to cancel.

        Deleting the row is the entire undo -- the purge function only ever
        looks at rows in this table, so once it's gone the account is simply
        a normal account again.
        """

        response = (
            user_client
            .table("account_deletion_requests")
            .delete()
            .eq("user_id", user_id)
            .execute()
        )
        return bool(response.data)

    def get_pending_account_deletion(self, user_client: Client, user_id: str) -> dict | None:
        """Return this user's pending deletion request, or None.

        Used by the settings page to swap the "Delete account" control for a
        "your account is scheduled for deletion on <date>" banner with a
        cancel button.
        """

        response = (
            user_client
            .table("account_deletion_requests")
            .select("requested_at,purge_after")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def delete_conversation(self, user_client: Client, conversation_id: str, user_id: str) -> None:
        """Permanently delete a conversation and all of its messages.

        messages.conversation_id has ON DELETE CASCADE, so deleting the
        conversation row is enough -- no separate messages delete needed.
        This cannot be undone (unlike archiving), which is why routes.py
        asks the user to confirm before calling it.
        """

        response = (
            user_client
            .table("conversations")
            .delete()
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not response.data:
            raise ValueError("Conversation not found for this user.")
