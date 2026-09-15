"""Create a one-chart input from the existing fixture for a memory run."""
from pathlib import Path
from pypdf import PdfReader, PdfWriter

source = Path("output/pdf/chart_extraction_test_fixture.pdf")
target = Path("output/pdf/chart_extraction_memory_input.pdf")
reader = PdfReader(source)
writer = PdfWriter()
writer.add_page(reader.pages[1])
with target.open("wb") as stream:
    writer.write(stream)
print(target)
