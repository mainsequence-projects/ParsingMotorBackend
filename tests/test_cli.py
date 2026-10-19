"""CLI cannot invoke the retired business workflow."""

import json

import pytest

from extractor import cli, document_models


@pytest.mark.parametrize("operation", ["recheck", "decide", "verify", "learn", "protocol", "extract"])
def test_business_commands_are_rejected(operation):
    with pytest.raises(SystemExit) as error:
        cli.main([operation, "old-run-id"])
    assert error.value.code == 2


def test_classification_cli_requires_catalog_and_uses_document_service(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    output = tmp_path / "output.json"
    payload = {"files": [{"filename": "a.png", "content_base64": "eA=="}],
               "document_types": [{"document_type": "passport", "description": "Travel document"}]}
    request.write_text(json.dumps(payload))
    def classify(body):
        assert isinstance(body, document_models.ClassifyJsonRequest)
        assert body.document_types[0].document_type == "passport"
        return {"pages": []}
    monkeypatch.setattr(cli.document_service, "discover", classify)
    assert cli.main(["classify", str(request), "-o", str(output)]) == 0
    assert json.loads(output.read_text()) == {"pages": []}


def test_files_alone_do_not_activate_an_implicit_loan_pipeline(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"files": [{"filename": "a.png", "content_base64": "eA=="}]}))
    with pytest.raises(SystemExit) as error:
        cli.main(["extract-typed", str(request)])
    assert error.value.code == 2
