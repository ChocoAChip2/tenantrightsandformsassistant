"""Supabase client wrapper used by the Flask routes.

This file centralizes all auth/data communication with Supabase so routes.py
can focus on request handling instead of client setup details.
"""

from dataclasses import dataclass

from supabase import Client, create_client

from config import Settings


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
            .insert({"user_id": user_id, "title": title})
            .execute()
        )
        return response.data[0]["id"]

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
        """Fetch full message history for a conversation, ordered oldest->newest."""

        response = (
            user_client
            .table("messages")
            .select("role,content,created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .execute()
        )
        return response.data or []

    def insert_message(self, user_client: Client, message: dict) -> None:
        """Insert a single message row under RLS."""

        user_client.table("messages").insert(message).execute()

    def list_conversations(self, user_client: Client) -> list[dict]:
        """Return user conversations sorted by most recent activity."""

        response = (
            user_client
            .table("conversations")
            .select("id,title,created_at,updated_at")
            .order("updated_at", desc=True)
            .execute()
        )
        return response.data or []
