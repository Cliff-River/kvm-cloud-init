"""固件选择测试。"""

from __future__ import annotations

import libvirt

from kvm_cloud_init.hv import firmware

_EFI_CAPS = """\
<domainCapabilities>
  <path>/usr/bin/qemu-system-x86_64</path>
  <os>
    <enum name='firmware'>
      <value>bios</value>
      <value>efi</value>
    </enum>
  </os>
</domainCapabilities>
"""

_BIOS_CAPS = """\
<domainCapabilities>
  <os supported='yes'/>
</domainCapabilities>
"""


class _FakeConn:
    def __init__(self, xml: str | None) -> None:
        self._xml = xml

    def getDomainCapabilities(self, *args):
        if self._xml is None:
            raise libvirt.libvirtError(None)
        return self._xml


def test_domain_caps_detects_efi() -> None:
    assert firmware._uefi_from_domain_caps(_FakeConn(_EFI_CAPS)) is True


def test_domain_caps_without_firmware_enum() -> None:
    assert firmware._uefi_from_domain_caps(_FakeConn(_BIOS_CAPS)) is False


def test_explicit_policy() -> None:
    assert firmware.choose_firmware("bios", _FakeConn(_EFI_CAPS)) == "bios"
    assert firmware.choose_firmware("uefi", _FakeConn(_BIOS_CAPS)) == "efi"


def test_auto_uses_detection(monkeypatch) -> None:
    monkeypatch.setattr(firmware, "uefi_available", lambda conn: True)
    assert firmware.choose_firmware("auto", object()) == "efi"
    monkeypatch.setattr(firmware, "uefi_available", lambda conn: False)
    assert firmware.choose_firmware("auto", object()) == "bios"


def test_filesystem_fallback(monkeypatch, tmp_path) -> None:
    # 域能力不可用 -> 回退文件系统扫描
    monkeypatch.setattr(firmware.glob, "glob", lambda pattern: [])
    assert firmware.uefi_available(_FakeConn(None)) is False

    ovmf = tmp_path / "OVMF_CODE.fd"
    ovmf.write_bytes(b"")
    monkeypatch.setattr(
        firmware.glob,
        "glob",
        lambda pattern: [str(ovmf)] if "OVMF_CODE" in pattern else [],
    )
    assert firmware.uefi_available(_FakeConn(None)) is True


def _desc(
    name: str,
    code: str,
    vars_tpl: str | None,
    features: list[str],
    *,
    device: str = "flash",
    machines: list[str] | None = None,
) -> tuple[str, dict]:
    """构造一份 qemu 固件描述符（文件名与内容）。"""
    mapping: dict = {
        "device": device,
        "executable": {"filename": code, "format": "raw"},
    }
    if vars_tpl is not None:
        mapping["nvram-template"] = {"filename": vars_tpl, "format": "raw"}
    return (
        name,
        {
            "interface-types": ["uefi"],
            "mapping": mapping,
            "targets": [
                {"architecture": "x86_64", "machines": machines or ["pc-q35-*"]}
            ],
            "features": features,
        },
    )


def _write_descriptors(tmp_path, descriptors: list[tuple[str, dict]]) -> None:
    import json

    desc_dir = tmp_path / "firmware"
    desc_dir.mkdir()
    for name, data in descriptors:
        (desc_dir / name).write_text(json.dumps(data), encoding="utf-8")


def test_resolve_plain_pair_picks_non_secureboot(monkeypatch, tmp_path) -> None:
    ovmf = tmp_path / "OVMF"
    ovmf.mkdir()
    for fname in (
        "OVMF_CODE_4M.fd",
        "OVMF_VARS_4M.fd",
        "OVMF_CODE_4M.secboot.fd",
        "OVMF_CODE_4M.ms.fd",
        "OVMF_VARS_4M.ms.fd",
    ):
        (ovmf / fname).write_bytes(b"")
    (tmp_path / "OVMF.amdsev.fd").write_bytes(b"")

    descriptors = [
        _desc(
            "40-enrolled.json",
            str(ovmf / "OVMF_CODE_4M.ms.fd"),
            str(ovmf / "OVMF_VARS_4M.ms.fd"),
            ["enrolled-keys", "requires-smm", "secure-boot"],
        ),
        _desc(
            "50-secure.json",
            str(ovmf / "OVMF_CODE_4M.secboot.fd"),
            str(ovmf / "OVMF_VARS_4M.fd"),
            ["requires-smm", "secure-boot"],
        ),
        _desc(
            "60-amdsev.json",
            str(tmp_path / "OVMF.amdsev.fd"),
            None,
            ["amd-sev", "amd-sev-es", "amd-sev-snp"],
            device="memory",
        ),
        _desc(
            "60-plain.json",
            str(ovmf / "OVMF_CODE_4M.fd"),
            str(ovmf / "OVMF_VARS_4M.fd"),
            ["acpi-s3", "amd-sev", "verbose-dynamic"],
            machines=["pc-i440fx-*", "pc-q35-*"],
        ),
    ]
    _write_descriptors(tmp_path, descriptors)
    monkeypatch.setattr(
        firmware, "_FIRMWARE_DESC_GLOBS", (str(tmp_path / "firmware" / "*.json"),)
    )

    assert firmware.resolve_plain_uefi_pair() == (
        str(ovmf / "OVMF_CODE_4M.fd"),
        str(ovmf / "OVMF_VARS_4M.fd"),
    )


def test_resolve_plain_pair_missing_files_returns_none(monkeypatch, tmp_path) -> None:
    descriptors = [
        _desc(
            "60-plain.json",
            str(tmp_path / "missing_CODE.fd"),
            str(tmp_path / "missing_VARS.fd"),
            ["acpi-s3"],
        ),
    ]
    _write_descriptors(tmp_path, descriptors)
    monkeypatch.setattr(
        firmware, "_FIRMWARE_DESC_GLOBS", (str(tmp_path / "firmware" / "*.json"),)
    )
    assert firmware.resolve_plain_uefi_pair() is None


def test_resolve_plain_pair_skips_broken_json(monkeypatch, tmp_path) -> None:
    desc_dir = tmp_path / "firmware"
    desc_dir.mkdir()
    (desc_dir / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        firmware, "_FIRMWARE_DESC_GLOBS", (str(desc_dir / "*.json"),)
    )
    assert firmware.resolve_plain_uefi_pair() is None


def test_resolve_plain_pair_no_descriptors(monkeypatch) -> None:
    monkeypatch.setattr(firmware, "_FIRMWARE_DESC_GLOBS", ("/nonexistent/*.json",))
    assert firmware.resolve_plain_uefi_pair() is None
