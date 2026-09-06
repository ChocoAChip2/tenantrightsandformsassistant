import os
from pypdf import PdfReader, PdfWriter

class FormService:
    @staticmethod
    def fill_tenant_form(json_data: dict, template_path: str, output_filename: str) -> str:
        reader = PdfReader(template_path)
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        
        # Note: We will need to update these keys (Text1, Text2) to match 
        # the exact hidden field names inside your specific PDF later.
        form_fields = {
            "Text1": json_data.get("name", ""),
            "Text2": json_data.get("address", ""),
            "Text3": json_data.get("complaint", "")
        }
        
        writer.update_page_form_field_values(writer.pages[0], form_fields)
        
        # Save to Render's temporary directory
        output_path = os.path.join("/tmp", output_filename)
        with open(output_path, "wb") as output_stream:
            writer.write(output_stream)
            
        return output_path
