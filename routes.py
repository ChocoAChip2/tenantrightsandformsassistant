"""HTTP routes for signup, login, chat, and logout pages.

app.py registers this blueprint, and each route uses the shared SupabaseService
stored in the Flask app config to handle authentication and chat persistence.
"""

import json
import logging
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for, send_file

from ai_service import AIService
from login_lockout import format_duration, record_failure, record_success, seconds_until_unlocked
from rate_limit import limiter, rate_limit_key
from supabase_service import SupabaseService
from form_service import FormService

# The blueprint groups the page routes together so app.py can register them as
# one unit.
main_bp = Blueprint("main", __name__)
logger = logging.getLogger(__name__)

# A message this long is well past anything a real tenant question needs and
# starts to look like someone testing how much they can push through a
# single request -- Gemini calls bill (and take longer) roughly in
# proportion to input size, so this caps both the cost and the latency risk
# of one oversized message, on top of the per-minute rate limit below.
MAX_MESSAGE_LENGTH = 8000

# Matches the sidebar's rename <input maxlength="120"> -- the client-side
# cap is just a nicer typing experience; this is the one that actually
# holds, since the client-side value is trivial to bypass.
MAX_CONVERSATION_TITLE_LENGTH = 120


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
@limiter.limit("10 per minute", methods=["POST"])
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
            flash(f"Sign-up failed: {exc}", "error")

    return render_template("signup.html")


@main_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    """Show the login page and create a browser session after authentication.

    Failed attempts are also tracked per-caller (see login_lockout.py): ten
    wrong passwords in a row locks that caller out of this route for an
    hour, doubling on each further lockout, on top of the per-minute rate
    limit above -- the rate limit alone resets every minute, which slows a
    scripted attack but doesn't stop one from just running slowly.
    """
    if request.method == "POST":
        supabase_service = get_supabase_service()
        lockout_key = rate_limit_key()
        wait_seconds = seconds_until_unlocked(lockout_key)
        if wait_seconds > 0:
            flash(
                f"Too many failed login attempts. Try again in {format_duration(wait_seconds)}.",
                "error",
            )
            return render_template("login.html")

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
            record_success(lockout_key)

            return redirect(url_for("main.chat"))
        except Exception as exc:
            record_failure(lockout_key)
            flash(
                f"Login failed. Confirm your email first if needed. Details: {exc}",
                "error",
            )

    return render_template("login.html")


@main_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def forgot_password():
    """Collect an email and ask Supabase to send it a password-reset link.

    The tight rate limit here isn't just abuse-of-this-app protection: this
    route makes Supabase send an email to whatever address is submitted, so
    with no limit at all it doubles as a free tool for spamming an
    arbitrary inbox with reset-password emails, or for brute-forcing
    Supabase's own outbound email quota.
    """

    if request.method == "POST":
        email = request.form.get("email", "").strip()

        if not email:
            flash("Please enter your email.", "error")
            return render_template("forgot_password.html")

        try:
            get_supabase_service().send_password_reset_email(
                email=email,
                redirect_to=url_for("main.reset_password", _external=True),
            )
        except Exception:
            # Deliberately swallowed: whether Supabase is unreachable, the
            # email doesn't exist, or anything else goes wrong, the visitor
            # sees the same message either way -- see the docstring on
            # send_password_reset_email for why this must not reveal
            # whether an account exists for this address. Real failures
            # (e.g. Supabase misconfigured) still land in the server logs.
            logger.exception("Failed to send password reset email.")

        flash(
            "If an account exists for that email, we've sent a link to reset your password.",
            "success",
        )
        return redirect(url_for("main.login"))

    return render_template("forgot_password.html")


@main_bp.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def reset_password():
    """Set a new password from the recovery link Supabase emailed the user.

    Supabase puts the recovery access/refresh tokens in the URL *fragment*
    (#access_token=...&refresh_token=...&type=recovery), which browsers
    never send to the server -- so reset_password.html reads them with
    JavaScript and copies them into hidden form fields before this route
    ever sees them. There is no logged-in session at this point (the
    visitor followed an emailed link), so this can't use session tokens
    the way settings.html's update_account does -- these tokens *are* the
    only proof of identity here, which is exactly how Supabase's recovery
    flow is designed to work.
    """

    if request.method == "POST":
        access_token = request.form.get("access_token", "")
        refresh_token = request.form.get("refresh_token", "")
        new_password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not access_token or not refresh_token:
            flash("This reset link is invalid or has expired. Request a new one below.", "error")
            return redirect(url_for("main.forgot_password"))

        if not new_password or new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html")

        if len(new_password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("reset_password.html")

        try:
            get_supabase_service().update_account(
                access_token=access_token,
                refresh_token=refresh_token,
                password=new_password,
            )
            flash("Your password has been reset. Please log in.", "success")
            return redirect(url_for("main.login"))
        except Exception:
            logger.exception("Failed to reset password.")
            flash(
                "Could not reset your password. The link may have expired -- request a new one.",
                "error",
            )
            return redirect(url_for("main.forgot_password"))

    return render_template("reset_password.html")


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
    conversations = supabase_service.list_conversations(user_client, archived=False)
    archived_conversations = supabase_service.list_conversations(user_client, archived=True)
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
        archived_conversations=archived_conversations,
        active_conversation_id=conversation_id,
        messages=messages,
    )


@main_bp.route("/conversations", methods=["POST"])
@limiter.limit("20 per minute")
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
    supabase_service = get_supabase_service()

    # Best-effort cleanup, not a precondition for creating the new one --
    # see delete_empty_conversations' docstring. A failure here shouldn't
    # stop the user from starting a new conversation just because the
    # sweep itself hit a problem.
    try:
        supabase_service.delete_empty_conversations(user_client, session["user_id"])
    except Exception:
        logger.exception("Failed to sweep empty conversations before creating a new one.")

    try:
        conversation_id = supabase_service.create_conversation(user_client, session["user_id"], title)
        return redirect(url_for("main.chat", conversation_id=conversation_id))
    except Exception:
        logger.exception("Failed to create conversation.")
        flash("Could not start a new conversation. Please try again.", "error")
        return redirect(url_for("main.chat"))


@main_bp.route("/conversations/<conversation_id>/archive", methods=["POST"])
def archive_conversation(conversation_id):
    """Soft-hide a conversation from the main sidebar list without deleting it."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    try:
        get_supabase_service().set_conversation_archived(
            user_client, conversation_id, session["user_id"], archived=True
        )
        flash("Conversation archived.", "success")
    except ValueError:
        flash("That conversation could not be found.", "error")
    except Exception:
        logger.exception("Failed to archive conversation.")
        flash("Could not archive that conversation. Please try again.", "error")

    return redirect(url_for("main.chat"))


@main_bp.route("/conversations/<conversation_id>/unarchive", methods=["POST"])
def unarchive_conversation(conversation_id):
    """Move a conversation back into the main sidebar list."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    try:
        get_supabase_service().set_conversation_archived(
            user_client, conversation_id, session["user_id"], archived=False
        )
        flash("Conversation restored.", "success")
    except ValueError:
        flash("That conversation could not be found.", "error")
    except Exception:
        logger.exception("Failed to unarchive conversation.")
        flash("Could not restore that conversation. Please try again.", "error")

    return redirect(url_for("main.chat"))


@main_bp.route("/conversations/<conversation_id>/rename", methods=["POST"])
@limiter.limit("20 per minute")
def rename_conversation(conversation_id):
    """Rename a conversation. The title shown in the sidebar is otherwise
    whatever the first message set it to (or "New conversation")."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    title = request.form.get("title", "").strip()[:MAX_CONVERSATION_TITLE_LENGTH]

    if not title:
        # The conversation being renamed is presumably still right there in
        # the sidebar, so send the visitor back into it to retry rather
        # than dropping them out to the plain chat list.
        flash("Conversation name cannot be empty.", "error")
        return redirect(url_for("main.chat", conversation_id=conversation_id))

    try:
        get_supabase_service().rename_conversation(user_client, conversation_id, session["user_id"], title)
        return redirect(url_for("main.chat", conversation_id=conversation_id))
    except ValueError:
        # Unlike the blank-title case above, the conversation itself is
        # what's missing here -- redirecting back into conversation_id
        # would just make /chat's own lookup immediately repeat this same
        # flash. Land on the plain chat list instead, matching
        # archive/unarchive/delete's error handling.
        flash("That conversation could not be found.", "error")
    except Exception:
        logger.exception("Failed to rename conversation.")
        flash("Could not rename that conversation. Please try again.", "error")

    return redirect(url_for("main.chat"))


@main_bp.route("/conversations/<conversation_id>/delete", methods=["POST"])
def delete_conversation(conversation_id):
    """Permanently delete a conversation and its messages. Cannot be undone."""

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    user_client = get_user_scoped_client()
    if not user_client:
        session.clear()
        flash("Your session expired. Please log in again.", "error")
        return redirect(url_for("main.login"))

    try:
        get_supabase_service().delete_conversation(user_client, conversation_id, session["user_id"])
        flash("Conversation deleted.", "success")
    except ValueError:
        flash("That conversation could not be found.", "error")
    except Exception:
        logger.exception("Failed to delete conversation.")
        flash("Could not delete that conversation. Please try again.", "error")

    # If the conversation that was just deleted was the active one, this
    # redirect (with no conversation_id) naturally lands back on the
    # greeting/empty state instead of a dead link.
    return redirect(url_for("main.chat"))


@main_bp.route("/chat/message", methods=["POST"])
@limiter.limit("20 per minute; 300 per day")
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

    if len(content) > MAX_MESSAGE_LENGTH:
        return jsonify(
            {"error": f"Message is too long (max {MAX_MESSAGE_LENGTH} characters)."}
        ), 400

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
@limiter.limit("10 per minute")
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


@main_bp.route("/logout", methods=["POST"])
def logout():
    """Clear the browser session and return the user to the login page.

    POST-only (and so CSRF-protected, like every other state-changing
    route) rather than a plain GET link -- a GET version could be forced
    on a signed-in visitor by any page that embeds e.g. <img
    src="/logout">, since browsers send GET requests for those without
    asking. Logging someone out uninvited doesn't expose or change any
    data, but there's no reason to leave a request forgery hole open once
    the fix is just a <form> instead of an <a>.
    """
    session.clear()
    return redirect(url_for("main.login"))
