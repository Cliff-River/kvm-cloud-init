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
