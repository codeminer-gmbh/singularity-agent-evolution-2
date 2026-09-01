Gap: The agent can produce text and spreadsheets but cannot create an editable PowerPoint deliverable for a briefing task.
Change: Add a bounded `create_presentation` MCP tool backed by python-pptx for titles, bullets, tables, and local images.
Priority: Presentation output is an unclosed file class with direct hard-task value, unlike already-inspected PDFs, emails, images, and spreadsheets.
