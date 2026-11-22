"""Local document reader invoked through the agent's ordinary command tools."""

from __future__ import annotations

import argparse
import codecs
import json
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import date, datetime, time
from itertools import islice
from pathlib import Path
from typing import Any, Iterator


TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".xml", ".html"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx", ".doc", ".rtf", ".xlsx", ".xlsm", ".xls"}


class LocalFileError(ValueError):
    """An actionable document-reading error suitable for CLI output."""


def reader_command() -> str:
    """Keep working when the project has a different Python environment."""
    return shlex.join([sys.executable, "-m", "mac_system.local_files"])


def _value(value: Any) -> str:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _text_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open(encoding=encoding) as stream:
                while stream.read(65536):
                    pass
            return encoding
        except UnicodeError:
            continue
    raise LocalFileError("无法识别文本编码；请先转换为 UTF-8。")


def _docx_sections(document: Any) -> Iterator[tuple[str, str]]:
    from docx.table import Table

    for index, block in enumerate(document.iter_inner_content(), 1):
        if isinstance(block, Table):
            text = "\n".join(json.dumps([cell.text for cell in row.cells], ensure_ascii=False) for row in block.rows)
            yield f"block {index} (table)", text
        else:
            yield f"block {index} (paragraph)", block.text


def read_local_file(
    filename: str | Path,
    *,
    start: int = 1,
    limit: int = 50,
    max_chars: int = 24000,
    sheet: str | None = None,
) -> dict[str, Any]:
    """Read a bounded preview. Start is a one-based page, body block or row.

    Excel formulas are preserved as source text; no formulas/macros are run.
    Reading a local path uses the caller's normal filesystem permissions.
    """
    if start < 1 or limit < 1 or max_chars < 1:
        raise LocalFileError("start、limit 和 max_chars 必须大于 0。")
    path = Path(filename).expanduser().resolve()
    if not path.is_file():
        raise LocalFileError(f"文件不存在或不是普通文件：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise LocalFileError(f"暂不支持 {suffix or '无扩展名'} 格式；图片请使用图片查看工具。")
    if sheet is not None and suffix not in {".xlsx", ".xlsm", ".xls"}:
        raise LocalFileError("--sheet 仅适用于 Excel 文件。")

    result: dict[str, Any] = {
        "path": str(path), "format": suffix[1:], "sections": [],
        "truncated": False, "next_start": None, "warnings": [],
    }
    warnings = result["warnings"]
    with ExitStack() as stack:
        more_after_limit = False
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(stack.enter_context(path.open("rb")))
            if reader.is_encrypted and not reader.decrypt(""):
                raise LocalFileError("PDF 受密码保护；请提供已解密的本地副本。")
            result["total_units"] = len(reader.pages)

            def pdf_sections() -> Iterator[tuple[str, str]]:
                for number in range(start, min(start + limit, len(reader.pages) + 1)):
                    text = reader.pages[number - 1].extract_text() or ""
                    if not text.strip():
                        warnings.append(f"第 {number} 页没有可提取文字，可能需要 OCR 或图片查看工具。")
                    yield f"page {number}", text

            sections = pdf_sections()
            more_after_limit = start + limit <= len(reader.pages)
        elif suffix == ".docx":
            from docx import Document

            warnings.append("读取 Word 正文段落和表格；图片、文本框、页眉页脚和批注需另行查看。")
            sections = islice(_docx_sections(Document(str(path))), start - 1, start - 1 + limit + 1)
        elif suffix in {".xlsx", ".xlsm", ".xls"}:
            warnings.append("不执行公式、宏或外部链接；xlsx/xlsm 显示公式原文，xls 显示文件中保存的结果。")
            if suffix == ".xls":
                import xlrd

                workbook = xlrd.open_workbook(str(path), on_demand=True)
                stack.callback(workbook.release_resources)
                names = workbook.sheet_names()
            else:
                from openpyxl import load_workbook

                workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
                stack.callback(workbook.close)
                names = workbook.sheetnames
            if not names:
                raise LocalFileError("Excel 文件没有工作表。")
            selected = sheet if sheet is not None else names[0]
            if selected not in names:
                raise LocalFileError(f"工作表不存在：{selected}；可用工作表：{names}")
            result.update(sheets=names, sheet=selected)
            if sheet is None and len(names) > 1:
                warnings.append("当前读取首个工作表；使用 --sheet 指定其他工作表。")

            if suffix == ".xls":
                worksheet = workbook.sheet_by_name(selected)
                result["total_units"] = worksheet.nrows

                def xls_sections() -> Iterator[tuple[str, str]]:
                    for index in range(start - 1, min(start - 1 + limit, worksheet.nrows)):
                        values = []
                        for cell in worksheet.row(index):
                            if cell.ctype == xlrd.XL_CELL_DATE:
                                values.append(xlrd.xldate_as_datetime(cell.value, workbook.datemode).isoformat())
                            elif cell.ctype == xlrd.XL_CELL_ERROR:
                                values.append(xlrd.error_text_from_code.get(cell.value, "#ERROR"))
                            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                                values.append(bool(cell.value))
                            else:
                                values.append(cell.value)
                        yield f"{selected}!row {index + 1}", json.dumps(values, ensure_ascii=False)

                sections = xls_sections()
                more_after_limit = start + limit <= worksheet.nrows
            else:
                worksheet = workbook[selected]
                # Some producers write incorrect sheet dimensions. Stream the
                # actual XML instead of silently accepting a truncated range.
                worksheet.reset_dimensions()
                row_iterator = worksheet.iter_rows(min_row=start, values_only=True)
                stack.callback(row_iterator.close)
                rows = islice(row_iterator, limit + 1)
                sections = (
                    (f"{selected}!row {index}", json.dumps(list(row), ensure_ascii=False, default=_value))
                    for index, row in enumerate(rows, start)
                )
        else:
            if suffix in {".doc", ".rtf"}:
                textutil = shutil.which("textutil")
                if not textutil:
                    raise LocalFileError("读取旧版 DOC/RTF 需要 macOS textutil；也可先转换为 DOCX。")
                converted = subprocess.run(
                    [textutil, "-convert", "txt", "-stdout", "-encoding", "UTF-8", str(path)],
                    capture_output=True, timeout=30, check=False,
                )
                if converted.returncode:
                    raise LocalFileError("DOC/RTF 转换失败；请检查文件是否损坏、加密，或先转换为 DOCX。")
                lines = iter(converted.stdout.decode("utf-8").splitlines())
                warnings.append("旧版 Word/RTF 已转换为纯文本，版式和图片未包含。")
            else:
                lines = iter(stack.enter_context(path.open(encoding=_text_encoding(path))))
            sections = (
                (f"line {index}", line.rstrip("\r\n"))
                for index, line in enumerate(islice(lines, start - 1, start - 1 + limit + 1), start)
            )

        remaining = max_chars
        for index, (location, text) in enumerate(sections, start):
            if index >= start + limit:
                result.update(truncated=True, next_start=index)
                break
            if len(text) > remaining:
                result["sections"].append({"location": location, "text": text[:remaining]})
                result.update(truncated=True, next_start=index)
                warnings.append(f"{location} 的内容未完整输出；请从 --start {index} 继续并提高 --max-chars。")
                break
            result["sections"].append({"location": location, "text": text})
            remaining -= len(text)
        else:
            if more_after_limit:
                result.update(truncated=True, next_start=start + limit)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读提取 PDF、Word、Excel 和常见文本文件，输出带位置的 JSON。")
    parser.add_argument("path", help="本地文件路径（含空格时请加引号）")
    parser.add_argument("--start", type=int, default=1, help="起始页/正文块/行，默认 1")
    parser.add_argument("--limit", type=int, default=50, help="最多读取多少页/正文块/行，默认 50")
    parser.add_argument("--max-chars", type=int, default=24000, help="正文输出字符上限，默认 24000")
    parser.add_argument("--sheet", help="Excel 工作表名称，默认首个工作表")
    args = parser.parse_args(argv)
    try:
        result = read_local_file(args.path, start=args.start, limit=args.limit, max_chars=args.max_chars, sheet=args.sheet)
    except Exception as exc:
        # Parser errors can contain document data. Keep unknown failures useful
        # without printing internal traces or file contents.
        message = str(exc) if isinstance(exc, LocalFileError) else (
            f"文件读取失败（{type(exc).__name__}）；请检查权限、文件格式、损坏或加密情况。"
        )
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
