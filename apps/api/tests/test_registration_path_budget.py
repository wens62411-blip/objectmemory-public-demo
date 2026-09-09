"""Registration crop staging must not exceed a valid destination path's budget."""
from pathlib import Path

import pytest

from apps.api.app.registration import RegistrationService


def service_at(root):
    service = RegistrationService.__new__(RegistrationService)
    service.root = root.resolve()
    service.folder = service.root / 'registered-items'
    service.folder.mkdir(parents=True)
    return service


def test_crop_staging_works_when_final_path_is_within_windows_limit(tmp_path):
    # A 249-character destination is valid without extended-path privileges;
    # appending another UUID to it is not. Use genuine filesystem writes.
    root = tmp_path / ('n' * max(1, 157 - len(str(tmp_path))))
    service = service_at(root)
    name = 'a' * 32 + '-' + 'b' * 32 + '-crop.jpg'
    target = service.folder / name
    assert len(str(target)) < 260
    original = service.folder / 'user-original.jpg'
    original.write_bytes(b'original-reference')
    url = service._write(name, b'complete-crop')
    assert url == '/media/registered-items/' + name
    assert target.read_bytes() == b'complete-crop'
    assert original.read_bytes() == b'original-reference'
    assert not list(service.folder.glob('*.tmp'))


def test_failed_atomic_replace_keeps_previous_reference_and_removes_temp(tmp_path, monkeypatch):
    service = service_at(tmp_path)
    target = service.folder / 'reference.jpg'
    target.write_bytes(b'previous-reference')
    staging = []

    def fail_replace(source, destination):
        staging.append(Path(source))
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes() == b'new-reference'
        raise OSError('controlled replace failure')

    monkeypatch.setattr('apps.api.app.registration.os.replace', fail_replace)
    with pytest.raises(OSError, match='controlled replace failure'):
        service._write(target.name, b'new-reference')
    assert target.read_bytes() == b'previous-reference'
    assert staging and not staging[0].exists()
