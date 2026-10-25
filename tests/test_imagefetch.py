"""imagefetch 纯能力层测试：目录页解析、校验和、下载与状态。

HTTP 全部通过 ``httpx.MockTransport`` 注入，不建立真实连接。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from kvm_cloud_init import imagefetch
from kvm_cloud_init.config import ImageSource
from kvm_cloud_init.errors import ImageFetchError

FIXTURES = Path(__file__).parent / "fixtures"


def make_client(
    routes: dict[str, bytes],
    headers: dict[str, dict[str, str]] | None = None,
) -> httpx.Client:
    """按 URL 返回固定内容的假客户端；未登记 URL 返回 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404, request=request)
        extra = (headers or {}).get(url, {})
        body = b"" if request.method == "HEAD" else routes[url]
        return httpx.Response(200, content=body, headers=extra, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_sleep(_seconds: float) -> None:
    return None


# ---- 压缩包解压 ----


def _make_tar(path: Path, member: str, payload: bytes) -> Path:
    import io
    import tarfile

    with tarfile.open(path, "w:xz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return path


def test_is_archive_recognises_supported_suffixes() -> None:
    assert imagefetch.is_archive("kali-linux-2026.3-cloud-genericcloud-amd64.tar.xz")
    assert imagefetch.is_archive("image.tar.gz")
    assert imagefetch.is_archive("image.zip")
    assert not imagefetch.is_archive("image.qcow2")
    assert not imagefetch.is_archive("image.raw")


def test_extract_archive_unpacks_single_member(tmp_path: Path) -> None:
    payload = b"HEAD" + b"\x00" * (2 << 20) + b"TAIL"
    archive = _make_tar(tmp_path / "disk.tar.xz", "disk.raw", payload)
    dest = tmp_path / "disk.raw"
    assert imagefetch.extract_archive(archive, dest) == "disk.raw"
    assert dest.read_bytes() == payload


def test_extract_archive_uses_logical_filename(tmp_path: Path) -> None:
    # 下载中的临时文件以 .part 结尾，压缩格式须由上游文件名判定
    payload = b"DATA" + b"\x00" * 4096
    archive = _make_tar(tmp_path / ".disk.qcow2.part", "disk.raw", payload)
    dest = tmp_path / "disk.raw"
    assert imagefetch.extract_archive(
        archive, dest, filename="disk-1.0.tar.xz"
    ) == "disk.raw"
    assert dest.read_bytes() == payload


def test_extract_archive_rejects_multiple_members(tmp_path: Path) -> None:
    import io
    import tarfile

    archive = tmp_path / "two.tar"
    with tarfile.open(archive, "w") as tf:
        for name in ("a.raw", "b.raw"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ImageFetchError, match="exactly one file, found 2"):
        imagefetch.extract_archive(archive, tmp_path / "out.raw")


def test_extract_archive_rejects_unsupported_container(tmp_path: Path) -> None:
    weird = tmp_path / "image.7z"
    weird.write_bytes(b"not really")
    with pytest.raises(ImageFetchError, match="Unsupported archive format"):
        imagefetch.extract_archive(weird, tmp_path / "out.raw")


# ---- 目录页解析 ----


def test_extract_hrefs_and_basename() -> None:
    html = (FIXTURES / "index_rocky.html").read_text()
    hrefs = imagefetch.extract_hrefs(html)
    assert "../" in hrefs
    assert imagefetch.basename("../") == ".."
    assert imagefetch.basename("a/b/file.qcow2?sort=1") == "file.qcow2"


def test_parse_index_absolute_urls_and_regex() -> None:
    html = (FIXTURES / "index_rocky.html").read_text()
    page = "https://example.test/rocky/10/images/x86_64/"
    candidates = imagefetch.parse_index(
        html, page, r"Rocky-10-GenericCloud-LVM-\d+\.\d+-\d{8}\.\d+\.x86_64\.qcow2$"
    )
    names = [item.filename for item in candidates]
    assert names == [
        "Rocky-10-GenericCloud-LVM-10.1-20260401.0.x86_64.qcow2",
        "Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2",
    ]
    assert candidates[-1].url == page + names[-1]
    assert "10.2-20260525.0" in candidates[-1].version


def test_pick_latest_by_name() -> None:
    html = (FIXTURES / "index_rocky.html").read_text()
    page = "https://example.test/"
    candidates = imagefetch.parse_index(
        html, page, r"Rocky-10-GenericCloud-LVM-\d+\.\d+-\d{8}\.\d+\.x86_64\.qcow2$"
    )
    chosen = imagefetch.pick_latest(candidates)
    assert chosen.filename.endswith("10.2-20260525.0.x86_64.qcow2")


def test_pick_latest_by_mtime_with_fake_head() -> None:
    candidates = [
        imagefetch.RemoteImage("a-1.qcow2", "https://x/a-1.qcow2", "1"),
        imagefetch.RemoteImage("a-2.qcow2", "https://x/a-2.qcow2", "2"),
    ]
    headers = {
        "https://x/a-1.qcow2": {"Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
        "https://x/a-2.qcow2": {"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
    }
    client = make_client({url: b"" for url in headers}, headers)
    chosen = imagefetch.pick_latest(
        candidates, "mtime", client=client, sleep=_no_sleep
    )
    assert chosen.filename == "a-1.qcow2"


def test_pick_latest_mtime_fallback_warns(capsys) -> None:
    candidates = [imagefetch.RemoteImage("a.qcow2", "https://x/a.qcow2", "a")]
    client = make_client({"https://x/a.qcow2": b""})
    chosen = imagefetch.pick_latest(
        candidates, "mtime", client=client, sleep=_no_sleep
    )
    assert chosen.filename == "a.qcow2"
    assert "falling back" in capsys.readouterr().err


# ---- 目标解析 ----


def test_resolve_target_url_kind() -> None:
    source = ImageSource(
        id="u",
        file="u.qcow2",
        kind="url",
        urls=("https://x/ubuntu-24.04-server-cloudimg-amd64.img",),
        checksum_url="https://x/SHA256SUMS",
    )
    target = imagefetch.resolve_target(source, sleep=_no_sleep)
    assert target.filename.endswith(".img")
    assert target.version.startswith("ubuntu-24.04")
    assert target.checksum_url == "https://x/SHA256SUMS"


def test_resolve_target_index_finds_checksum_file() -> None:
    html = (FIXTURES / "index_rocky.html").read_text()
    page = "https://example.test/rocky/10/images/x86_64/"
    source = ImageSource(
        id="rocky-10-lvm",
        file="Rocky-10-LVM.qcow2",
        kind="index",
        pages=(page,),
        match=r"Rocky-10-GenericCloud-LVM-\d+\.\d+-\d{8}\.\d+\.x86_64\.qcow2$",
        checksum_file="CHECKSUM",
    )
    target = imagefetch.resolve_target(
        source, client=make_client({page: html.encode()}), sleep=_no_sleep
    )
    assert target.filename.endswith("10.2-20260525.0.x86_64.qcow2")
    assert target.checksum_url == page + "CHECKSUM"


def test_resolve_target_index_uses_checksum_match() -> None:
    html = (FIXTURES / "index_rocky.html").read_text()
    page = "https://example.test/fedora/"
    source = ImageSource(
        id="fedora",
        file="fedora.qcow2",
        kind="index",
        pages=(page,),
        match=r"Rocky-10-GenericCloud-LVM-\d+\.\d+-\d{8}\.\d+\.x86_64\.qcow2$",
        checksum_match=r"CHECKSUM$",
    )
    target = imagefetch.resolve_target(
        source, client=make_client({page: html.encode()}), sleep=_no_sleep
    )
    assert target.checksum_url == page + "CHECKSUM"


def test_resolve_target_index_errors_when_nothing_matches() -> None:
    page = "https://example.test/empty/"
    source = ImageSource(
        id="empty",
        file="x.qcow2",
        kind="index",
        pages=(page,),
        match=r"nothing-matches",
        checksum_file="CHECKSUM",
    )
    with pytest.raises(ImageFetchError, match="No usable image"):
        imagefetch.resolve_target(
            source, client=make_client({page: b"<html></html>"}), sleep=_no_sleep
        )


# ---- 校验和 ----


def test_parse_checksum_supports_coreutils_and_bsd() -> None:
    text = (FIXTURES / "checksums.txt").read_text()
    rocky = imagefetch.parse_checksum(
        text, "Rocky-9-GenericCloud-LVM-9.8-20260525.0.x86_64.qcow2", "sha256"
    )
    assert rocky == "fedcba9876543210" * 4
    debian = imagefetch.parse_checksum(
        text, "debian-13-generic-amd64.qcow2", "sha256"
    )
    assert debian == "0123456789abcdef" * 4


def test_parse_checksum_rejects_unknown_file() -> None:
    with pytest.raises(ImageFetchError, match="No sha256 checksum"):
        imagefetch.parse_checksum("", "missing.qcow2", "sha256")


def test_parse_checksum_rejects_ambiguous_and_bad_length() -> None:
    line = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  x.qcow2"
    with pytest.raises(ImageFetchError, match="Ambiguous"):
        imagefetch.parse_checksum(f"{line}\n{line}\n", "x.qcow2", "sha256")
    with pytest.raises(ImageFetchError, match="unexpected length"):
        imagefetch.parse_checksum("abc  x.qcow2\n", "x.qcow2", "sha256")


def test_fetch_checksum_without_url_returns_none() -> None:
    target = imagefetch.RemoteTarget(
        filename="x.qcow2", urls=("https://x/x.qcow2",), checksum_url=None, version="x"
    )
    assert imagefetch.fetch_checksum(target, sleep=_no_sleep) is None


# ---- 下载 ----


def _target(url: str, algo: str = "sha256") -> imagefetch.RemoteTarget:
    return imagefetch.RemoteTarget(
        filename="x.qcow2", urls=(url,), checksum_url=None, version="x", algo=algo
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[int, int | None]] = []
        self.closed = False

    def update(self, done: int, total: int | None) -> None:
        self.events.append((done, total))

    def close(self) -> None:
        self.closed = True


def test_download_writes_part_and_returns_hash(tmp_path: Path) -> None:
    body = b"cloud image bytes" * 1000
    expected = hashlib.sha256(body).hexdigest()
    part = tmp_path / ".x.qcow2.part"
    progress = _Recorder()
    result = imagefetch.download(
        _target("https://x/x.qcow2"),
        part,
        expected=expected,
        client=make_client(
            {"https://x/x.qcow2": body},
            {"https://x/x.qcow2": {"Content-Length": str(len(body))}},
        ),
        progress=progress,
        sleep=_no_sleep,
    )
    assert result.checksum == expected
    assert result.size == len(body)
    assert part.read_bytes() == body
    assert (0, len(body)) in progress.events
    assert progress.events[-1] == (len(body), len(body))
    assert progress.closed


def test_download_uses_head_size_when_get_is_chunked(tmp_path: Path) -> None:
    body = b"chunked image bytes"
    expected = hashlib.sha256(body).hexdigest()
    progress = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                request=request,
            )
        # 模拟 chunked：GET 不返回 Content-Length
        return httpx.Response(200, content=body, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    imagefetch.download(
        _target("https://x/x.qcow2"),
        tmp_path / ".x.qcow2.part",
        expected=expected,
        client=client,
        progress=progress,
        sleep=_no_sleep,
    )
    assert (0, len(body)) in progress.events


def test_download_mismatch_keeps_existing_and_removes_part(tmp_path: Path) -> None:
    final = tmp_path / "x.qcow2"
    final.write_bytes(b"old complete image")
    part = tmp_path / ".x.qcow2.part"
    progress = _Recorder()
    with pytest.raises(ImageFetchError, match="checksum mismatch"):
        imagefetch.download(
            _target("https://x/x.qcow2"),
            part,
            expected="0" * 64,
            client=make_client({"https://x/x.qcow2": b"new broken image"}),
            progress=progress,
            sleep=_no_sleep,
        )
    assert final.read_bytes() == b"old complete image"
    assert not part.exists()
    assert progress.closed


def test_download_falls_back_to_second_url(tmp_path: Path) -> None:
    body = b"payload"
    expected = hashlib.sha256(body).hexdigest()
    target = imagefetch.RemoteTarget(
        filename="x.qcow2",
        urls=("https://x/missing.qcow2", "https://x/x.qcow2"),
        checksum_url=None,
        version="x",
    )
    result = imagefetch.download(
        target,
        tmp_path / ".x.qcow2.part",
        expected=expected,
        client=make_client({"https://x/x.qcow2": body}),
        sleep=_no_sleep,
    )
    assert result.checksum == expected


def test_download_fails_without_free_space(tmp_path: Path) -> None:
    body = b"x" * 1024
    with pytest.raises(ImageFetchError, match="Not enough free disk space"):
        imagefetch.download(
            _target("https://x/x.qcow2"),
            tmp_path / ".x.qcow2.part",
            client=make_client(
                {"https://x/x.qcow2": body},
                {"https://x/x.qcow2": {"Content-Length": str(len(body))}},
            ),
            free_space=lambda _path: 1,
            sleep=_no_sleep,
        )
    assert not (tmp_path / ".x.qcow2.part").exists()


def test_download_keyboard_interrupt_removes_part(tmp_path: Path) -> None:
    class _InterruptingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"first chunk"
            raise KeyboardInterrupt

        def close(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_InterruptingStream(), request=request
        )

    part = tmp_path / ".x.qcow2.part"
    progress = _Recorder()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(KeyboardInterrupt):
        imagefetch.download(
            _target("https://x/x.qcow2"),
            part,
            client=client,
            progress=progress,
            sleep=_no_sleep,
        )
    assert not part.exists()
    assert progress.closed


# ---- 状态文件 ----


def test_load_state_missing_returns_empty(tmp_path: Path) -> None:
    assert imagefetch.load_state(tmp_path) == {}


def test_load_state_corrupt_warns_and_returns_empty(tmp_path: Path, capsys) -> None:
    (tmp_path / imagefetch.STATE_FILENAME).write_text("{not json")
    assert imagefetch.load_state(tmp_path) == {}
    assert "Warning" in capsys.readouterr().err


def test_save_state_is_atomic_json(tmp_path: Path) -> None:
    imagefetch.save_state(tmp_path, {"a": {"file": "a.qcow2"}})
    raw = json.loads((tmp_path / imagefetch.STATE_FILENAME).read_text())
    assert raw == {"a": {"file": "a.qcow2"}}
    assert not (tmp_path / (imagefetch.STATE_FILENAME + ".tmp")).exists()
