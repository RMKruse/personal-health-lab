import sys
from pathlib import Path

from personal_health_lab.storage import (
    CapacityStatus,
    FileVaultReason,
    FileVaultStatus,
    probe_capacity,
    probe_filevault,
)


def test_real_macos_platform_probes_succeed_and_redact(tmp_path: Path) -> None:
    filevault = probe_filevault(tmp_path)
    capacity = probe_capacity(tmp_path, 0)

    assert isinstance(filevault.status, FileVaultStatus)
    if sys.platform == "darwin":
        assert filevault.reason not in {
            FileVaultReason.MOUNT_UNRESOLVED,
            FileVaultReason.DISK_INFO_UNAVAILABLE,
            FileVaultReason.VOLUME_IDENTITY_MISSING,
            FileVaultReason.APFS_STATE_UNAVAILABLE,
            FileVaultReason.FILEVAULT_STATE_UNAVAILABLE,
            FileVaultReason.FILEVAULT_STATE_UNREPORTED,
            FileVaultReason.PROBE_FAILED,
        }
    else:
        assert filevault.reason is FileVaultReason.UNSUPPORTED_PLATFORM
    assert capacity.status is CapacityStatus.READY
    assert capacity.available_bytes is not None
    assert str(tmp_path) not in repr(filevault)
    assert str(tmp_path) not in repr(capacity)
