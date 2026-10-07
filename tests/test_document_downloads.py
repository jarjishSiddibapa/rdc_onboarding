"""
Document download routes on the request detail page (2026-10-07):
per-document "Download" and a "Download all (.zip)" bundle, both gated by the
exact same access rules as viewing the request itself.
"""
import io
import uuid
import zipfile
from app.models import UserRole, RequestStatus, OnboardingRequest
from .conftest import login, logout, _make_user


def _req_with_docs(db, app, tmp_path, owner, company="RDC", status=RequestStatus.PENDING_BH,
                   names=("aadhar.pdf", "pan.pdf")):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    docs = []
    for i, name in enumerate(names):
        stored = f"{uuid.uuid4().hex}.pdf"
        (tmp_path / stored).write_bytes(f"content-{i}".encode())
        docs.append({"type": f"doc{i}", "label": f"Doc {i}", "name": name,
                     "filename": stored, "url": f"/static/uploads/{stored}"})
    req = OnboardingRequest(
        initiated_by=owner.id, status=status, public_token=uuid.uuid4().hex,
        candidate_name="Asha Rao", company_code=company, plant_location="Plant A",
        designation="Engineer",
    )
    db.session.add(req)
    db.session.flush()
    req.form_data = {"company_code": company, "associate_name": "Asha Rao"}
    req.documents = docs
    db.session.commit()
    return req, docs


class TestSingleDocumentDownload:
    def test_initiator_can_download_own_document_as_attachment(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit1", "dlinit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init)
            token = req.public_token
            login(client, init.email)
            r = client.get(f"/requests/{token}/documents/0/download")
            assert r.status_code == 200
            assert r.data == b"content-0"
            assert "attachment" in r.headers["Content-Disposition"]
            assert "aadhar.pdf" in r.headers["Content-Disposition"]

    def test_detail_page_shows_download_links(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit2", "dlinit2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init)
            token = req.public_token
            login(client, init.email)
            html = client.get(f"/requests/{token}").get_data(as_text=True)
            assert f"/requests/{token}/documents/0/download" in html
            assert f"/requests/{token}/documents/1/download" in html
            assert f"/requests/{token}/documents/download-all" in html

    def test_single_document_has_no_zip_button(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit3", "dlinit3@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init, names=("only.pdf",))
            token = req.public_token
            login(client, init.email)
            html = client.get(f"/requests/{token}").get_data(as_text=True)
            assert f"/requests/{token}/documents/0/download" in html
            assert "download-all" not in html

    def test_out_of_range_index_is_404(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit4", "dlinit4@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init)
            token = req.public_token
            login(client, init.email)
            assert client.get(f"/requests/{token}/documents/9/download").status_code == 404

    def test_missing_file_on_disk_is_404(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit5", "dlinit5@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, docs = _req_with_docs(db, app, tmp_path, init)
            token = req.public_token
            (tmp_path / docs[0]["filename"]).unlink()
            login(client, init.email)
            assert client.get(f"/requests/{token}/documents/0/download").status_code == 404

    def test_path_traversal_in_stored_filename_is_refused(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit6", "dlinit6@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, docs = _req_with_docs(db, app, tmp_path, init, names=("a.pdf",))
            secret = tmp_path.parent / "secret.txt"
            secret.write_text("nope")
            docs[0]["filename"] = "../secret.txt"
            req.documents = docs
            db.session.commit()
            token = req.public_token
            login(client, init.email)
            r = client.get(f"/requests/{token}/documents/0/download")
            assert r.status_code == 404
            assert b"nope" not in r.data

    def test_login_required(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("DlInit7", "dlinit7@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init)
            r = client.get(f"/requests/{req.public_token}/documents/0/download")
            assert r.status_code in (301, 302)
            assert "/auth/login" in r.headers["Location"]


class TestDownloadAllZip:
    def test_zip_contains_every_document(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("ZipInit1", "zipinit1@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init)
            token = req.public_token
            login(client, init.email)
            r = client.get(f"/requests/{token}/documents/download-all")
            assert r.status_code == 200
            assert r.mimetype == "application/zip"
            assert "Documents_Asha_Rao_" in r.headers["Content-Disposition"]
            zf = zipfile.ZipFile(io.BytesIO(r.data))
            assert sorted(zf.namelist()) == ["aadhar.pdf", "pan.pdf"]
            assert zf.read("aadhar.pdf") == b"content-0"
            assert zf.read("pan.pdf") == b"content-1"

    def test_duplicate_original_names_are_both_kept(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("ZipInit2", "zipinit2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init, names=("scan.pdf", "scan.pdf"))
            token = req.public_token
            login(client, init.email)
            zf = zipfile.ZipFile(io.BytesIO(client.get(f"/requests/{token}/documents/download-all").data))
            assert sorted(zf.namelist()) == ["scan (2).pdf", "scan.pdf"]
            assert {zf.read(n) for n in zf.namelist()} == {b"content-0", b"content-1"}

    def test_request_without_documents_is_404(self, client, db, app, tmp_path):
        with app.app_context():
            init = _make_user("ZipInit3", "zipinit3@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, init, names=())
            token = req.public_token
            login(client, init.email)
            assert client.get(f"/requests/{token}/documents/download-all").status_code == 404


class TestDownloadAccessMatchesViewAccess:
    def _both(self, client, token):
        return (client.get(f"/requests/{token}/documents/0/download").status_code,
                client.get(f"/requests/{token}/documents/download-all").status_code)

    def test_other_initiator_is_forbidden(self, client, db, app, tmp_path):
        with app.app_context():
            owner = _make_user("AcOwner", "acowner@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            other = _make_user("AcOther", "acother@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, owner)
            token = req.public_token
            login(client, other.email)
            assert self._both(client, token) == (403, 403)

    def test_business_head_not_ticked_for_company_is_forbidden(self, client, db, app, tmp_path):
        with app.app_context():
            owner = _make_user("AcOwner2", "acowner2@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            bh = _make_user("AcBhRobo", "acbhrobo@t.com", UserRole.BUSINESS_HEAD, db, companies=["ROBO"])
            req, _ = _req_with_docs(db, app, tmp_path, owner)
            token = req.public_token
            login(client, bh.email)
            assert self._both(client, token) == (403, 403)

    def test_business_head_ticked_for_company_can_download(self, client, db, app, tmp_path):
        with app.app_context():
            owner = _make_user("AcOwner3", "acowner3@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            bh = _make_user("AcBhRdc", "acbhrdc@t.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, owner)
            token = req.public_token
            login(client, bh.email)
            assert self._both(client, token) == (200, 200)

    def test_draft_is_private_to_its_initiator(self, client, db, app, tmp_path):
        with app.app_context():
            owner = _make_user("AcOwner4", "acowner4@t.com", UserRole.INITIATOR, db, companies=["RDC"])
            hrm = _make_user("AcHrm", "achrm@t.com", UserRole.HR_MANAGER, db, companies=["RDC"])
            req, _ = _req_with_docs(db, app, tmp_path, owner, status=RequestStatus.DRAFT)
            token = req.public_token
            login(client, hrm.email)
            assert self._both(client, token) == (403, 403)
            logout(client)
            login(client, owner.email)
            assert self._both(client, token) == (200, 200)
