import json
import shlex
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from mac_system.feishu.bridge import FeishuBridge, FeishuBridgeError
from mac_system.feishu.models import MessageAttachment
from mac_system.feishu.ws_client import _parse_text_message


@pytest.mark.parametrize("kind,content,expected_text,expected", [
    ("file", {"file_key": "file-key", "file_name": "报告.pdf"}, "", [("file-key", "file", "报告.pdf")]),
    ("image", {"image_key": "image-key"}, "", [("image-key", "image", "照片")]),
    ("post", {"title": "看看图片", "content": [[{"tag": "text", "text": "描述"}, {"tag": "img", "image_key": "img-1"}], [{"tag": "img", "image_key": "img-2"}]]}, "看看图片\n描述", [("img-1", "image", "照片"), ("img-2", "image", "照片")]),
    ("post", {"zh_cn": {"content": [[{"tag": "text", "text": "@_user_1 阅读"}, {"tag": "img", "image_key": "img-1"}]]}}, "阅读", [("img-1", "image", "照片")]),
    ("text", {"text": "原始问题"}, "原始问题", []),
])
@pytest.mark.asyncio
async def test_http_and_sdk_parse_the_same_attachments(kind, content, expected_text, expected):
    bridge = FeishuBridge("test-app", "test-secret")
    sender = {"sender_id": {"open_id": "ou-user", "user_id": "user-1", "union_id": "union-1"}, "sender_type": "user"}
    message = {"message_id": "om-1", "message_type": kind, "content": json.dumps(content), "chat_id": "oc-chat", "parent_id": "om-card"}
    try:
        http_message = bridge.parse_text_message({"event": {"sender": sender, "message": message}})
        sdk_message = _parse_text_message(SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(**sender["sender_id"]), sender_type="user"),
            message=SimpleNamespace(**message),
        )))
        for parsed in (http_message, sdk_message):
            assert parsed.text == expected_text
            assert [(item.key, item.kind, item.name) for item in parsed.attachments] == expected
            assert parsed.open_id == "ou-user"
            assert parsed.user_id == "user-1"
            assert parsed.parent_id == "om-card"
    finally:
        await bridge.close()


async def resource_bridge(handler):
    bridge = FeishuBridge("test-app", "test-secret")
    await bridge._client.aclose()
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bridge._token = "test-token"
    bridge._token_expire_at = float("inf")
    return bridge


@pytest.mark.asyncio
async def test_download_uses_original_message_resource_and_keeps_json_files(tmp_path):
    data = b'{"code": 42, "msg": "user data"}'

    def handler(request):
        assert request.url.raw_path.startswith(b"/open-apis/im/v1/messages/om%2F1/resources/key%2F1")
        assert request.url.params["type"] == "file"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, content=data, headers={"content-type": "application/json", "content-disposition": 'attachment; filename="data.json"'})

    bridge = await resource_bridge(handler)
    destination = tmp_path / "data.json"
    try:
        assert await bridge.download_message_resource("om/1", MessageAttachment("key/1", "file", "data.json"), destination) == "application/json"
        assert destination.read_bytes() == data
        assert bridge._client_requests == {}
    finally:
        await bridge.close()


class ChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"1234"
        yield b"5678"


@pytest.mark.parametrize("failure", ["declared_size", "streamed_size", "permission", "api_error", "empty"])
@pytest.mark.asyncio
async def test_failed_download_is_bounded_and_removes_partial_file(tmp_path, failure):
    def handler(request):
        assert request.url.params["type"] == "image"
        if failure == "permission":
            return httpx.Response(403, json={"msg": "sensitive internal details"})
        if failure == "api_error":
            return httpx.Response(200, json={"code": 234001, "msg": "missing scope"})
        if failure == "declared_size":
            return httpx.Response(200, content=b"12345678")
        if failure == "empty":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, stream=ChunkedBody())

    bridge = await resource_bridge(handler)
    destination = tmp_path / "photo"
    try:
        with pytest.raises(FeishuBridgeError) as exc:
            await bridge.download_message_resource("om-1", MessageAttachment("image-key", "image", "照片"), destination, max_bytes=100 if failure == "api_error" else 6)
        assert "sensitive internal details" not in str(exc.value)
        assert not destination.exists()
        assert bridge._client_requests == {}
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_download_never_overwrites_an_existing_file(tmp_path):
    bridge = await resource_bridge(lambda request: httpx.Response(200, content=b"new"))
    destination = tmp_path / "original.txt"
    destination.write_bytes(b"original")
    try:
        with pytest.raises(FileExistsError):
            await bridge.download_message_resource("om-1", MessageAttachment("file-key", "file", "original.txt"), destination)
        assert destination.read_bytes() == b"original"
    finally:
        await bridge.close()


@pytest.mark.parametrize("suffix", ["docx", "xlsx"])
@pytest.mark.asyncio
async def test_document_attachment_provides_a_working_native_read_command(tmp_path, suffix):
    from docx import Document
    from openpyxl import Workbook
    from mac_system.feishu.handlers import _prepare_attachment_input

    source = tmp_path / f"source.{suffix}"
    if suffix == "docx":
        document = Document()
        document.add_paragraph("飞书文档正文")
        document.save(source)
    else:
        workbook = Workbook()
        workbook.active.append(["飞书文档正文", 3])
        workbook.save(source)

    async def download(message_id, attachment, destination):
        destination.write_bytes(source.read_bytes())
        return "application/octet-stream"

    workspace = tmp_path / "bound-project"
    workspace.mkdir()
    project = SimpleNamespace(id="project-1", workspace_path=str(workspace))
    context = SimpleNamespace(bridge=SimpleNamespace(download_message_resource=download))
    message = SimpleNamespace(message_id="om-doc", attachments=[MessageAttachment("key", "file", f"需求 '$name.{suffix}")])
    root_text = await _prepare_attachment_input(context, project, message, "读取内容")
    attached = json.loads(root_text.rsplit("\n", 1)[1])[0]
    command = root_text.split("普通命令工具运行 ", 1)[1].split(" <文件路径>", 1)[0]
    process = subprocess.run(shlex.split(command) + [attached["path"]], cwd=workspace, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert "飞书文档正文" in result["sections"][0]["text"]
    assert result["truncated"] is False
    assert result["path"].startswith(str(workspace / ".mac" / "attachments" / "project-1"))
