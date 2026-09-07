"""Tests for RA-81 form filling.

This form is filed with a state housing agency, so the thing these tests
actually guard is that values land in the *right* boxes. Two bugs made it
here before: the code wrote to a field name ("Text1") that does not exist
in the PDF at all, and it built the output with append_pages_from_reader(),
which copies pages but not the /AcroForm dictionary -- so nothing was
written regardless of the names used, and the feature shipped producing
blank forms.
"""

import os
import unittest

from pypdf import PdfReader

from form_service import (
    COMPLAINT_FIELD,
    COMPLAINT_OVERFLOW_FIELD,
    TEMPLATE_PATH,
    TENANT_APT_FIELD,
    TENANT_CITY_STATE_ZIP_FIELD,
    TENANT_NAME_FIELD,
    TENANT_STREET_FIELD,
    FormService,
    split_address,
)

OWNER_FIELDS = ["Name_2", "NumberStreet", "State Zip Code_2", "Text5"]


def _fill(**overrides):
    data = {
        "status": "complete",
        "name": "Maria Rodriguez",
        "address": "350 Grand Concourse Apt 4B, Bronx, NY 10451",
        "complaint": "No heat since November 3rd.",
    }
    data.update(overrides)
    path = FormService.fill_tenant_form(data)
    fields = PdfReader(path).get_fields() or {}
    return path, {name: str(f["/V"]) for name, f in fields.items() if f.get("/V")}


class TemplateTests(unittest.TestCase):
    def test_the_template_exists_and_is_resolved_independently_of_cwd(self):
        self.assertTrue(os.path.isabs(TEMPLATE_PATH))
        self.assertTrue(os.path.exists(TEMPLATE_PATH))

    def test_every_field_this_code_writes_to_actually_exists_in_the_pdf(self):
        """The original bug: 'Text1' was never a field on this form."""
        available = set((PdfReader(TEMPLATE_PATH).get_fields() or {}).keys())
        for field in [
            TENANT_NAME_FIELD, TENANT_STREET_FIELD, TENANT_APT_FIELD,
            TENANT_CITY_STATE_ZIP_FIELD, COMPLAINT_FIELD, COMPLAINT_OVERFLOW_FIELD,
        ]:
            self.assertIn(field, available, f"{field!r} is not a field on the RA-81")


class FilledValueTests(unittest.TestCase):
    def test_values_are_actually_written(self):
        """Guards the /AcroForm regression: this returned an empty dict when
        the writer was built with append_pages_from_reader()."""
        _, values = _fill()
        self.assertTrue(values, "no field values were written into the PDF")

    def test_the_tenant_details_land_in_the_tenant_boxes(self):
        _, values = _fill()
        self.assertEqual(values[TENANT_NAME_FIELD], "Maria Rodriguez")
        self.assertEqual(values[TENANT_STREET_FIELD], "350 Grand Concourse")
        self.assertEqual(values[TENANT_APT_FIELD], "4B")
        self.assertEqual(values[TENANT_CITY_STATE_ZIP_FIELD], "Bronx, NY 10451")

    def test_nothing_is_written_into_the_owner_column(self):
        """The form has two 'Name' fields side by side. Putting the tenant's
        name in the landlord's box would be worse than leaving it blank."""
        _, values = _fill()
        for field in OWNER_FIELDS:
            self.assertNotIn(field, values)

    def test_the_complaint_lands_on_the_page_two_complaint_lines(self):
        _, values = _fill(complaint="The radiator in the living room has been cold since October.")
        self.assertIn("radiator", values[COMPLAINT_FIELD])

    def test_a_long_complaint_says_so_instead_of_silently_truncating(self):
        _, values = _fill(complaint="Detail. " * 120)
        combined = values[COMPLAINT_FIELD] + values[COMPLAINT_OVERFLOW_FIELD]
        self.assertIn("continued in attached statement", combined)

    def test_missing_fields_do_not_crash_or_write_empty_boxes(self):
        _, values = _fill(name="", address="", complaint="")
        self.assertEqual(values, {})


class OutputFileTests(unittest.TestCase):
    def test_each_call_writes_a_separate_file(self):
        """The old fixed /tmp/completed_complaint.pdf was shared by every
        request in the process: two tenants finishing intake at the same
        time could race, and one would download the other's complaint."""
        first, _ = _fill(name="Tenant One")
        second, _ = _fill(name="Tenant Two")

        self.assertNotEqual(first, second)
        self.assertEqual(str((PdfReader(first).get_fields())[TENANT_NAME_FIELD]["/V"]), "Tenant One")
        self.assertEqual(str((PdfReader(second).get_fields())[TENANT_NAME_FIELD]["/V"]), "Tenant Two")

    def test_viewers_are_asked_to_render_the_filled_values(self):
        """Without NeedAppearances many viewers show the boxes empty even
        though the values are in the file."""
        path, _ = _fill()
        root = PdfReader(path).trailer["/Root"]
        self.assertTrue(root["/AcroForm"].get("/NeedAppearances"))


class AddressSplittingTests(unittest.TestCase):
    def test_splits_street_apartment_and_city_state_zip(self):
        self.assertEqual(
            split_address("350 Grand Concourse Apt 4B, Bronx, NY 10451"),
            ("350 Grand Concourse", "4B", "Bronx, NY 10451"),
        )

    def test_handles_an_address_with_no_apartment(self):
        self.assertEqual(split_address("55 Fake St, Queens, NY 11101"), ("55 Fake St", "", "Queens, NY 11101"))

    def test_anything_unparseable_stays_on_the_street_line(self):
        """Better one oddly-formatted line than a zip code landing in the
        apartment box."""
        self.assertEqual(split_address("somewhere vague"), ("somewhere vague", "", ""))

    def test_empty_address(self):
        self.assertEqual(split_address(""), ("", "", ""))
        self.assertEqual(split_address(None), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
