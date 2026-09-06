"""HTTP routes for signup, login, chat, and logout pages.

app.py registers this blueprint, and each route uses the shared SupabaseService
stored in the Flask app config to handle authentication and chat persistence.
"""

import json
import logging
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for, send_file

from ai_service import AIService
from supabase_service import SupabaseService
from form_service import FormService

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
        supabase_service = get_supabase_service()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template("signup.html")

        try:
            supabase_service.sign_up(email=email, password=password)
            flash(
                "Sign-up successful. Please confirm your email, then log in.",
                "success",
            )
            return redirect(url_for("main.login"))
        except Exception as exc:
            flash(f"Sign-up failed: {exc}", "error")

    return render_template("signup.html")


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    """Show the login page and create a browser session after authentication."""
    if request.method == "POST":
        supabase_service = get_supabase_service()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return render_template("login.html")

        try:
            auth_response = supabase_service.sign_in(email=email, password=password)
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

        # Check if the AI outputted the final JSON payload
        try:
            parsed_response = json.loads(reply)
            
            if parsed_response.get("status") == "complete":
                # Build the completed PDF
                pdf_path = FormService.fill_tenant_form(
                    json_data=parsed_response,
                    template_path="templates/ra-81-fillable.pdf",
                    output_filename="completed_complaint.pdf"
                )
                
                # Trigger the browser download
                return send_file(
                    pdf_path,
                    as_attachment=True,
                    download_name="NYC_Tenant_Complaint.pdf",
                    mimetype="application/pdf"
                )
                
        except json.JSONDecodeError:
            # If it is not JSON, it is a normal chat response. Send it to the frontend.
            pass

        return jsonify({"reply": reply})

    except ValueError:
        return jsonify({"error": "No valid messages were provided."}), 400
    except RuntimeError:
        return jsonify({"error": "AI service is not configured yet."}), 503
    except Exception:
        logger.exception("Failed to generate AI response.")
        return jsonify({"error": "The AI service is currently unavailable. Please try again shortly."}), 500


@main_bp.route("/settings")
def settings():
    """Show the settings page: appearance, account, and data export."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    return render_template("settings.html", user_email=session["user_email"])


@main_bp.route("/settings/account", methods=["POST"])
def update_account():
    """Update the signed-in user's email and/or password via Supabase auth."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not access_token or not refresh_token:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    new_email = request.form.get("email", "").strip()
    new_password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_email and not new_password:
        flash("Enter a new email and/or password to update your account.", "error")
        return redirect(url_for("main.settings"))

    if new_password and new_password != confirm_password:
        flash("New password and confirmation do not match.", "error")
        return redirect(url_for("main.settings"))

    if new_password and len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("main.settings"))

    try:
        get_supabase_service().update_account(
            access_token=access_token,
            refresh_token=refresh_token,
            email=new_email or None,
            password=new_password or None,
        )
        if new_email:
            flash(f"Check {new_email} for a confirmation link before the new email takes effect.", "success")
        if new_password:
            flash("Password updated.", "success")
    except Exception as exc:
        logger.exception("Failed to update account.")
        flash(f"Could not update account: {exc}", "error")

    return redirect(url_for("main.settings"))


@main_bp.route("/settings/download-logs")
def download_chat_history():
    """Export every conversation the signed-in user has as a Markdown file."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    conversations = get_supabase_service().fetch_all_conversations_with_messages(user_client)
    content = _render_conversations_as_markdown(conversations)

    response = current_app.response_class(content, mimetype="text/markdown")
    response.headers["Content-Disposition"] = "attachment; filename=nyc-tenant-assistant-chat-history.md"
    return response


def _render_conversations_as_markdown(conversations: list[dict]) -> str:
    """Turn a list of conversations (each with a "messages" list) into one Markdown document."""

    lines = ["# NYC Tenant Assistant -- Chat History Export", ""]

    if not conversations:
        lines.append("_No conversations yet._")

    for conversation in conversations:
        lines.append(f"## {conversation.get('title') or 'Untitled conversation'}")
        lines.append(f"_Conversation ID: {conversation['id']} -- created {conversation.get('created_at', 'unknown')}_")
        lines.append("")
        for message in conversation.get("messages", []):
            speaker = "You" if message.get("role") == "user" else "Assistant"
            lines.append(f"**{speaker}:** {message.get('content', '')}")
            lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


@main_bp.route("/logout")
def logout():
    """Clear the browser session and return the user to the login page."""
    session.clear()
    return redirect(url_for("main.login"))
