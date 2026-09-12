from parser.src.docling.document_identity import identity_metadata, make_document_id
from parser.src.docling.type_format import DocumentMetadata


def test_metadata_identity_uses_fixed_order_and_normalization():
    first = DocumentMetadata(
        doi=" DOI:10.1000/ABC ",
        title="  A   Paper ",
        authors=["Alice Smith"],
        organization="Example Org",
        publication_date="2024-01-02",
    )
    second = DocumentMetadata(
        doi="doi:10.1000/abc",
        title="a paper",
        authors=[" alice   smith "],
        organization="example org",
        publication_date="2024-01-02",
    )
    assert identity_metadata(first) == identity_metadata(second)
    assert make_document_id(first, run_id="run-a", filename="one.pdf") == make_document_id(
        second, run_id="run-b", filename="two.pdf"
    )


def test_fallback_identity_uses_run_and_filename_only_when_metadata_is_empty():
    metadata = DocumentMetadata()
    first = make_document_id(metadata, run_id="run-a", filename="paper.pdf")
    same = make_document_id(metadata, run_id="run-a", filename="/other/paper.pdf")
    different_run = make_document_id(metadata, run_id="run-b", filename="paper.pdf")
    different_name = make_document_id(metadata, run_id="run-a", filename="other.pdf")
    assert first == same
    assert first != different_run
    assert first != different_name
