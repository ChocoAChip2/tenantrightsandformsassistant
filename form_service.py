"""Fills the DHCR RA-81 form ("Application For A Rent Reduction Based Upon
Decreased Service(s) - Individual Apartment") from the intake JSON the
assistant produces.

FIELD NAMES ARE NOT GUESSABLE FROM THIS FORM -- READ BEFORE EDITING.
The RA-81's internal field names are mostly what Acrobat auto-generated,
so they are useless as documentation: the tenant's street address is
called "Text2", the apartment number "Text3", and there are two fields
called "Name" and "Name_2" for the tenant and the owner respectively.
This mapping was derived by extracting each widget's rectangle from the
PDF and matching it against the rendered page, not by reading the names:

  page 1, tenant column (x ~= 76-131)      page 1, owner column (x ~= 351-391)
    Name             tenant name             Name_2            owner name
    Text2            number/street           NumberStreet      owner street
    Text3            apt. no. (narrow)       State Zip Code_2  owner city/state/zip
    State Zip Code   city, state, zip        Text5             owner telephone
    Text4            telephone (business)
    Text6            telephone (residence)
    Text8            subject building line

  page 2, complaint lines
    Kitchen / Bathroom / "Bedroom Specify..." / "Living Room" /
    "Dining Room" / "Hall Inside Apartment" / "Other Specify which room
    and the problem", each with an unnamed continuation line
    (undefined_5, _7, _9, _11, _13, _15).

If you change this mapping, regenerate a filled sample and LOOK at it.
This is a document a tenant files with a state agency; a plausible-looking
form with the name in the landlord's box is worse than an empty one.
"""

import os
import re
import tempfile

from pypdf import PdfWriter

# Resolved against this file, not the process working directory -- the
# previous relative path only worked because gunicorn happens to start in
# the repo root.
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "ra-81-fillable.pdf")

TENANT_NAME_FIELD = "Name"
TENANT_STREET_FIELD = "Text2"
TENANT_APT_FIELD = "Text3"
TENANT_CITY_STATE_ZIP_FIELD = "State Zip Code"
COMPLAINT_FIELD = "Other Specify which room and the problem"
# Named for the heading that follows it, not for what it is: this is the
# continuation line directly under "Other (Specify which room and the
# problem)". Verified by widget position -- it sits at y=269, below the
# "Other" line at y=287. "undefined_15" (y=310) is NOT this line; that one
# belongs to "Hall Inside Apartment" above it.
COMPLAINT_OVERFLOW_FIELD = "Part III  Tenants Affirmation"

# What the two complaint lines hold before the text would run off the page.
COMPLAINT_LINE_CHARS = 120
COMPLAINT_OVERFLOW_CHARS = 230
_TRUNCATION_NOTICE = " (continued in attached statement)"

# "... Brooklyn, NY 11216", "... Apt 4B, Bronx, NY 10453"
_CITY_STATE_ZIP = re.compile(r",\s*([^,]+,\s*(?:NY|New York)\s*\d{5}(?:-\d{4})?)\s*$", re.I)
_APARTMENT = re.compile(r"\b(?:apt\.?|apartment|unit|#)\s*([A-Za-z0-9\-]+)", re.I)


def split_address(address: str) -> tuple[str, str, str]:
    """Best-effort split of one free-text address into (street, apt, city/state/zip).

    The assistant collects the address as a single sentence, but the form
    has three separate boxes for it. Anything this cannot confidently
    separate stays in the street line rather than being dropped or guessed
    into the wrong box -- an address that reads a little oddly on one line
    is recoverable; a zip code silently landing in the apartment box is
    not.
    """
    address = (address or "").strip()
    if not address:
        return "", "", ""

    city_state_zip = ""
    match = _CITY_STATE_ZIP.search(address)
    if match:
        city_state_zip = match.group(1).strip()
        address = address[: match.start()].strip().rstrip(",")

    apartment = ""
    apt_match = _APARTMENT.search(address)
    if apt_match:
        apartment = apt_match.group(1).strip()
        address = (address[: apt_match.start()] + address[apt_match.end():]).strip().rstrip(",").strip()

    return address, apartment, city_state_zip


class FormService:
    @staticmethod
    def fill_tenant_form(json_data: dict, template_path: str | None = None, output_filename: str | None = None) -> str:
        """Fill the RA-81 and return a path to the completed file.

        Every call writes to its own uniquely-named temporary file.
        The previous version wrote to a single fixed path
        (/tmp/completed_complaint.pdf) shared by every request in the
        process, so two tenants completing intake at the same time could
        race -- and the loser would download the other one's complaint.
        `output_filename` is accepted for backwards compatibility and used
        only as a filename hint.
        """
        # clone_from copies the whole document, including the /AcroForm
        # dictionary. The previous version used append_pages_from_reader(),
        # which brings the pages and their widget annotations but NOT the
        # form catalog those widgets belong to -- so
        # update_page_form_field_values() had no field tree to write into
        # and silently wrote nothing. That is why this produced a blank
        # form regardless of which field names were used.
        writer = PdfWriter(clone_from=template_path or TEMPLATE_PATH)

        # Without this, many viewers (including Preview and most browsers'
        # built-in PDF readers) show the boxes empty: the values are in the
        # file, but nothing has generated the visual appearance streams for
        # them. This asks the viewer to render them.
        writer.set_need_appearances_writer(True)

        street, apartment, city_state_zip = split_address(json_data.get("address", ""))
        complaint = (json_data.get("complaint") or "").strip()

        first_line = complaint[:COMPLAINT_LINE_CHARS]
        rest = complaint[COMPLAINT_LINE_CHARS:]
        second_line = rest[:COMPLAINT_OVERFLOW_CHARS]
        # Two ruled lines is all the form gives this. Rather than silently
        # dropping the tail of someone's complaint -- on a document they
        # file with a state agency -- say plainly that it continues
        # elsewhere, which is also what the form's own instructions tell
        # tenants to do with supporting detail.
        if len(rest) > COMPLAINT_OVERFLOW_CHARS:
            second_line = second_line[: COMPLAINT_OVERFLOW_CHARS - len(_TRUNCATION_NOTICE)].rstrip() + _TRUNCATION_NOTICE

        values = {
            TENANT_NAME_FIELD: json_data.get("name", ""),
            TENANT_STREET_FIELD: street,
            TENANT_APT_FIELD: apartment,
            TENANT_CITY_STATE_ZIP_FIELD: city_state_zip,
            COMPLAINT_FIELD: first_line,
            COMPLAINT_OVERFLOW_FIELD: second_line,
        }
        values = {name: value for name, value in values.items() if value}

        # The tenant's details are on page 1 and the complaint lines on
        # page 2, so every page gets a pass. The old version only wrote to
        # page 1, which meant the complaint text was silently discarded
        # even once the field names were right.
        for page in writer.pages:
            try:
                writer.update_page_form_field_values(page, values)
            except Exception:
                # A page with no matching widgets is normal, not an error.
                continue

        prefix = os.path.splitext(output_filename or "completed_complaint")[0]
        handle, output_path = tempfile.mkstemp(prefix=f"{prefix}_", suffix=".pdf")
        with os.fdopen(handle, "wb") as stream:
            writer.write(stream)
        return output_path
