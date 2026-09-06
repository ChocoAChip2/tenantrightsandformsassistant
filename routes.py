"""HTTP routes for signup, login, chat, and logout pages.

app.py registers this blueprint, and each route uses the shared SupabaseService
stored in the Flask app config to handle authentication and chat persistence.
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
import logging

from ai_service import AIService
from supabase_service import SupabaseService

# The blueprint groups the page routes together so app.py can register them as
# one unit.
main_bp = Blueprint("main", __name__)
logger = logging.getLogger(__name__)


def get_supabase_service() -> SupabaseService:
    """Fetch the shared service object that app.py stored on the Flask app."""

    return current_app.config["SUPABASE_SERVICE"]


def get_ai_service() -> AIService:
    """Fetch the shared AI service object that app.py stored on the Flask app."""

    return current_app.config["AI_SERVICE"]


def get_user_scoped_client():
    """Build an RLS-scoped Supabase client from the session's access token.

    Returns None if there is no token or the token is no longer valid, so
    callers can send the visitor back to login instead of hitting Supabase
    with a request that RLS will just reject anyway.
    """

    access_token = session.get("access_token")
    if not access_token:
        return None

    try:
        return get_supabase_service().build_user_scoped_client(access_token)
    except Exception:
        logger.exception("Failed to build user-scoped Supabase client.")
        return None


@main_bp.route("/", methods=["GET", "POST"])
def signup():
    """Show the signup page and create a new account on form submission."""

    if request.method == "POST":
        # Use the service built in app.py so request handlers do not recreate the
        # Supabase client on every form submission.
        supabase_service = get_supabase_service()

        # Pull form values from the signup.html template.
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        # Stop early if the template form was submitted without both fields.
        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template("signup.html")

        try:
            # Ask Supabase to create the account. sign_up() returns False
            # instead of raising when the email is already registered, since
            # Supabase itself won't always tell us that directly (see the
            # docstring in supabase_service.py) -- either way, we must not
            # tell the visitor to "check your email" for an account that
            # already exists and never got a new confirmation email.
            created = supabase_service.sign_up(email=email, password=password)
            if not created:
                return render_template("signup.html", existing_account_email=email)

            flash(
                "Sign-up successful. Please confirm your email, then log in.",
                "success",
            )
            return redirect(url_for("main.login"))
        except Exception as exc:
            # Show the SDK or configuration error on the same page.
            flash(f"Sign-up failed: {exc}", "error")

    return render_template("signup.html")


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    """Show the login page and create a browser session after authentication."""

    if request.method == "POST":
        supabase_service = get_supabase_service()

        # Pull form values from the login.html template.
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template("login.html")

        try:
            auth_response = supabase_service.sign_in(email=email, password=password)

            # Store both the identity and the Supabase session tokens: the chat
            # routes need the access token to build an RLS-scoped client so
            # conversation/message queries are enforced per-user by Postgres,
            # not just by application code.
            session["user_email"] = auth_response.user.email
            session["user_id"] = auth_response.user.id
            session["access_token"] = auth_response.session.access_token
            session["refresh_token"] = auth_response.session.refresh_token

            return redirect(url_for("main.chat"))
        except Exception as exc:
            flash(
                f"Login failed. Confirm your email first if needed. Details: {exc}",
                "error",
            )

    return render_template("login.html")


@main_bp.route("/chat")
def chat():
    """Render the chat page: a conversation list plus the selected conversation."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    supabase_service = get_supabase_service()
    conversations = supabase_service.list_conversations(user_client)

    conversation_id = request.args.get("conversation_id")
    messages = []

    if conversation_id:
        try:
            supabase_service.ensure_conversation_for_user(user_client, conversation_id, session["user_id"])
            messages = supabase_service.fetch_messages_for_conversation(user_client, conversation_id)
        except ValueError:
            flash("That conversation could not be found.", "error")
            conversation_id = None

    return render_template(
        "chat.html",
        user_email=session["user_email"],
        ai_ready=get_ai_service().is_ready(),
        conversations=conversations,
        active_conversation_id=conversation_id,
        messages=messages,
    )


@main_bp.route("/conversations", methods=["POST"])
def create_conversation():
    """Create a new named conversation and jump straight into it."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    title = request.form.get("title", "").strip() or "New conversation"

    try:
        conversation_id = get_supabase_service().create_conversation(user_client, session["user_id"], title)
        return redirect(url_for("main.chat", conversation_id=conversation_id))
    except Exception:
        logger.exception("Failed to create conversation.")
        flash("Could not start a new conversation. Please try again.", "error")
        return redirect(url_for("main.chat"))


@main_bp.route("/chat/message", methods=["POST"])
def chat_message():
    """Persist a user message, generate an AI reply from the stored history, and persist that too."""

    if not session.get("user_id"):
        return jsonify({"error": "Please log in first."}), 401

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        return jsonify({"error": "Your session expired. Please log in again."}), 401

    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get("conversation_id")
    content = (payload.get("content") or "").strip()

    if not conversation_id or not content:
        return jsonify({"error": "A conversation and message are required."}), 400

    supabase_service = get_supabase_service()
    user_id = session["user_id"]

    try:
        supabase_service.ensure_conversation_for_user(user_client, conversation_id, user_id)
    except ValueError:
        return jsonify({"error": "Conversation not found."}), 404

    try:
        supabase_service.insert_message(user_client, {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": "user",
            "content": content,
        })

        # Read the history back from Supabase rather than trusting anything the
        # client sent, so the model only ever sees turns that were actually
        # persisted (and a client can't spoof prior "assistant" replies).
        history = supabase_service.fetch_messages_for_conversation(user_client, conversation_id)
        reply = get_ai_service().generate_reply(
            [{"role": message["role"], "content": message["content"]} for message in history]
        )

        supabase_service.insert_message(user_client, {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": "assistant",
            "content": reply,
        })

        return jsonify({"reply": reply})
    except ValueError:
        return jsonify({"error": "No valid messages were provided."}), 400
    except RuntimeError:
        return jsonify({"error": "AI service is not configured yet."}), 503
    except Exception:
        logger.exception("Failed to generate AI response.")
        return jsonify({"error": "The AI service is currently unavailable. Please try again shortly."}), 500


@main_bp.route("/logout")
def logout():
    """Clear the browser session and return the user to the login page."""

    session.clear()
    return redirect(url_for("main.login"))
