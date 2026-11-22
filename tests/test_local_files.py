import hashlib
import json
import shlex
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mac_system.local_files import LocalFileError, main, read_local_file, reader_command


def write_pdf(path, texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)


def test_pdf_pages_pagination_and_source_unchanged(tmp_path):
    path = tmp_path / "report with spaces.PDF"
    write_pdf(path, ["First page", "Second page", "Third page"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    result = read_local_file(path, start=2, limit=1)
    assert result["sections"] == [{"location": "page 2", "text": "Second page"}]
    assert result["total_units"] == 3
    assert result["next_start"] == 3
    assert result["truncated"] is True
    last = read_local_file(path, start=result["next_start"])
    assert last["sections"][0]["text"] == "Third page"
    assert last["truncated"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_empty_pdf_and_password_are_explicit(tmp_path):
    path = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.write(path)
    result = read_local_file(path)
    assert result["sections"][0]["text"] == ""
    assert "OCR" in result["warnings"][0]

    writer.encrypt("private-password")
    writer.write(path)
    with pytest.raises(LocalFileError, match="密码保护"):
        read_local_file(path)


def test_docx_preserves_paragraph_table_order_and_chinese(tmp_path):
    path = tmp_path / "需求说明.docx"
    document = Document()
    document.add_paragraph("项目背景")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "产品"
    table.cell(0, 1).text = "数量"
    table.cell(1, 0).text = "相机"
    table.cell(1, 1).text = "3"
    document.add_paragraph("后续要求")
    document.save(path)

    result = read_local_file(path, limit=2)
    assert result["sections"][0]["text"] == "项目背景"
    assert result["sections"][1]["location"] == "block 2 (table)"
    assert json.loads(result["sections"][1]["text"].splitlines()[1]) == ["相机", "3"]
    assert result["next_start"] == 3
    assert read_local_file(path, start=3)["sections"][0]["text"] == "后续要求"


@pytest.mark.parametrize("suffix", [".xlsx", ".xlsm"])
def test_excel_sheets_rows_formulas_and_dates(tmp_path, suffix):
    path = tmp_path / ("经营数据" + suffix)
    workbook = Workbook()
    workbook.active.title = "目录"
    workbook.active.append(["说明"])
    sheet = workbook.create_sheet("销售 数据")
    sheet.append(["名称", "数量", "计算", "日期"])
    sheet.append(["相机", 3, "=B2*10", datetime(2026, 11, 22)])
    sheet.append(["镜头", 5])
    workbook.save(path)

    initial = read_local_file(path)
    assert initial["sheets"] == ["目录", "销售 数据"]
    assert initial["sheet"] == "目录"
    assert initial["truncated"] is False
    assert any("--sheet" in warning for warning in initial["warnings"])
    result = read_local_file(path, sheet="销售 数据", start=2, limit=1)
    assert result["sections"][0]["location"] == "销售 数据!row 2"
    assert json.loads(result["sections"][0]["text"]) == ["相机", 3, "=B2*10", "2026-11-22T00:00:00"]
    assert result["next_start"] == 3
    assert read_local_file(path, sheet="销售 数据", start=3)["truncated"] is False
    with pytest.raises(LocalFileError, match="工作表不存在"):
        read_local_file(path, sheet="不存在")


def test_xlsx_does_not_trust_incorrect_dimensions(tmp_path):
    original = tmp_path / "original.xlsx"
    path = tmp_path / "incorrect.xlsx"
    workbook = Workbook()
    workbook.active.append(["first", 1])
    workbook.active.append(["second", 2])
    workbook.save(original)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(b'<dimension ref="A1:B2"', b'<dimension ref="A1:A1"')
            target.writestr(item, content)
    result = read_local_file(path)
    assert len(result["sections"]) == 2
    assert json.loads(result["sections"][1]["text"]) == ["second", 2]


def test_legacy_xls_reads_real_binary_workbook():
    path = Path(__file__).parent / "fixtures" / "local_files" / "legacy.xls"
    result = read_local_file(path, sheet="销售", start=2, limit=1)
    assert json.loads(result["sections"][0]["text"]) == ["相机", 3.0, "2026-11-22T00:00:00", True]
    assert result["next_start"] == 3
    assert read_local_file(path, sheet="销售", start=3)["truncated"] is False


@pytest.mark.skipif(not shutil.which("textutil"), reason="legacy Word conversion needs macOS textutil")
@pytest.mark.parametrize("suffix", ["doc", "rtf"])
def test_legacy_word_using_real_textutil(tmp_path, suffix):
    text = tmp_path / "source.txt"
    text.write_text("本地文件读取\n第二行", encoding="utf-8")
    document = tmp_path / ("旧版文档." + suffix)
    subprocess.run([shutil.which("textutil"), "-convert", suffix, "-output", str(document), str(text)], check=True, capture_output=True)
    result = read_local_file(document)
    assert "本地文件读取" in result["sections"][0]["text"]
    assert "第二行" in result["sections"][1]["text"]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_text_encodings_and_bounded_continuation(tmp_path, encoding):
    path = tmp_path / "数据.csv"
    path.write_text("产品,数量\n相机,3\n镜头,5", encoding=encoding)
    result = read_local_file(path, limit=1)
    assert result["sections"] == [{"location": "line 1", "text": "产品,数量"}]
    assert result["next_start"] == 2
    assert read_local_file(path, start=2)["sections"][0]["text"] == "相机,3"


def test_character_limit_marks_partial_unit_and_allows_rereading(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("abcdefgh\nsecond", encoding="utf-8")
    result = read_local_file(path, max_chars=3)
    assert result["sections"][0]["text"] == "abc"
    assert result["truncated"] is True
    assert result["next_start"] == 1
    assert any("--max-chars" in warning for warning in result["warnings"])
    assert read_local_file(path, start=1, max_chars=100)["sections"][0]["text"] == "abcdefgh"


def test_cli_works_outside_host_workspace_without_initializing_db(tmp_path):
    path = tmp_path / "本地文件 ' $name.txt"
    path.write_text("完整内容", encoding="utf-8")
    process = subprocess.run(shlex.split(reader_command()) + [str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["sections"][0]["text"] == "完整内容"
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("filename,content,expected", [
    ("missing.pdf", None, "文件不存在"),
    ("binary.bin", b"\x00\x01", "暂不支持"),
    ("corrupt.docx", b"not a document", "文件读取失败"),
    ("corrupt.pdf", b"not a pdf", "文件读取失败"),
])
def test_cli_errors_without_false_success_or_tracebacks(tmp_path, capsys, filename, content, expected):
    path = tmp_path / filename
    if content is not None:
        path.write_bytes(content)
    assert main([str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in json.loads(captured.err)["error"]
    assert "Traceback" not in captured.err


def test_invalid_read_range_and_sheet_are_rejected(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("data")
    with pytest.raises(LocalFileError, match="必须大于 0"):
        read_local_file(path, start=0)
    with pytest.raises(LocalFileError, match="仅适用于 Excel"):
        read_local_file(path, sheet="Sheet1")


def test_batch_cli_parses_mixed_documents_and_isolates_damaged_files(tmp_path, capsys):
    pdf = tmp_path / "报告.pdf"
    write_pdf(pdf, ["PDF batch content"])
    word = tmp_path / "说明.docx"
    document = Document()
    document.add_paragraph("Word 批量正文")
    document.save(word)
    excel = tmp_path / "表格.xlsx"
    workbook = Workbook()
    workbook.active.append(["Excel 批量数据", 42])
    workbook.save(excel)
    damaged = tmp_path / "损坏.pdf"
    damaged.write_bytes(b"not a pdf")
    missing = tmp_path / "missing.docx"

    # A failure in the middle must not hide the later Excel result.
    assert main([str(pdf), str(word), str(damaged), str(excel), str(missing)]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert (result["succeeded"], result["failed"]) == (3, 2)
    assert "PDF batch content" in result["files"][0]["sections"][0]["text"]
    assert "Word 批量正文" in result["files"][1]["sections"][0]["text"]
    assert "Excel 批量数据" in result["files"][3]["sections"][0]["text"]
    assert result["files"][2]["path"] == str(damaged)
    assert "error" in result["files"][2]
    assert "error" in result["files"][4]
    assert "Traceback" not in captured.err


def test_batch_cli_bounds_total_text_and_preserves_per_file_pagination(tmp_path, capsys):
    paths = [tmp_path / f"{index}.txt" for index in range(3)]
    for path in paths:
        path.write_text("abcdef\nsecond line\n")
    assert main([*(str(path) for path in paths), "--max-chars", "9"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["failed"] == 0
    assert sum(len(section["text"]) for file in result["files"] for section in file["sections"]) == 9
    for file in result["files"]:
        assert file["truncated"] is True
        assert file["next_start"] == 1
        assert file["warnings"]


def test_batch_cli_sanitizes_parser_failures_and_continues(tmp_path, capsys, monkeypatch):
    from mac_system import local_files

    actual_read = local_files.read_local_file
    valid = tmp_path / "valid.txt"
    valid.write_text("still parsed")

    def read(path, **kwargs):
        if path == "broken.pdf":
            raise RuntimeError("private document contents")
        return actual_read(path, **kwargs)

    monkeypatch.setattr(local_files, "read_local_file", read)
    assert main(["broken.pdf", str(valid)]) == 1
    captured = capsys.readouterr()
    assert "private document contents" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert "RuntimeError" in result["files"][0]["error"]
    assert result["files"][1]["sections"][0]["text"] == "still parsed"
