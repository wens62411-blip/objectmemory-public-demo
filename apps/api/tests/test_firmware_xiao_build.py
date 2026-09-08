"""Build/publication contracts with compiler doubles, never hardware evidence."""
import configparser
import hashlib
import json
from pathlib import Path

import pytest

from apps.api.tests.test_firmware import load_script, _seed_current_firmware_bundle


ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENT = 'seeed_xiao_esp32s3'
OFFSETS = {'bootloader.bin': 0, 'partitions.bin': 0x8000, 'boot_app0.bin': 0xE000, 'firmware.bin': 0x10000}


@pytest.fixture
def build_context(tmp_path, monkeypatch):
    builder = load_script('xiao_isolated_build_contract', 'scripts/firmware-build.py')
    repo = tmp_path/'repo'
    ai_current, ai_public = _seed_current_firmware_bundle(repo, b'protected-ai-build')
    build_root = tmp_path/'build-core'
    boot = build_root/'platformio-core/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin'
    boot.parent.mkdir(parents=True)
    boot.write_bytes(b'explicit-test-boot-app-not-flashable')
    monkeypatch.setattr(builder, 'repository_root', lambda: repo)
    monkeypatch.setattr(builder, 'safe_build_root', lambda: build_root)
    monkeypatch.setattr(builder, 'firmware_python', lambda _repo=None: repo/'compiler-double.exe')
    monkeypatch.setattr(builder, '_platformio_version', lambda *_args: 'explicit compiler double')
    monkeypatch.setattr(builder, 'enumerate_eligible_ports', lambda: pytest.fail('Build must not enumerate ports'))
    state = {'commands': [], 'missing': None, 'failed': False}
    def compile_double(command, cwd, env, stream):
        assert (ai_public/'.build.lock').is_file()
        assert not (cwd/'firmware/build').exists()
        assert not (cwd/'firmware/build-xiao-esp32s3-sense').exists()
        state['commands'].append(command)
        if state['failed']: return 4
        output = cwd/'firmware/.pio/build'/command[command.index('--environment')+1]
        output.mkdir(parents=True)
        for name in ('firmware.bin', 'bootloader.bin', 'partitions.bin', 'firmware.elf'):
            if name != state['missing']: (output/name).write_bytes(('compiler-double-'+name).encode())
        stream.write('RAM: [=] 1.0% (used 1 bytes from 100 bytes)\n')
        return 0
    monkeypatch.setattr(builder, '_run_and_tee', compile_double)
    return builder, repo, ai_current, ai_public, state


def ai_bytes(current, public):
    return {(path.parent.name, path.name): path.read_bytes() for path in current.iterdir() if path.is_file()} | {
        ('public', 'manifest.json'): (public/'manifest.json').read_bytes()}


def test_xiao_build_and_success_archive_do_not_replace_ai_thinker_bundle(build_context):
    builder, repo, ai_current, ai_public, state = build_context
    protected = ai_bytes(ai_current, ai_public)
    manifest = builder.execute(flash=False, port=None, clean=False, environment=ENVIRONMENT)
    current = repo/'firmware/esp32cam/build-xiao-esp32s3-sense'
    public = ai_public/'xiao-esp32s3-sense'
    assert manifest['environment'] == ENVIRONMENT
    assert manifest['board_type'] == manifest['board_model'] == 'xiao_esp32s3_sense'
    assert manifest['target_board'] == 'Seeed XIAO ESP32S3 Sense'
    assert manifest['chip'] == 'esp32s3' and manifest['image_chip_id'] == 9
    assert manifest['flash_size_mb'] == manifest['psram_size_mb'] == 8
    assert manifest['flash_offsets'] == OFFSETS
    assert manifest['camera_auth'] == 'hmac-sha256-v1'
    assert manifest['compile_passed'] and not manifest['flash_attempted'] and not manifest['physical_flash_performed']
    assert {item['name'] for item in manifest['artifacts']} == {*OFFSETS, 'firmware.elf'}
    for item in manifest['artifacts']:
        assert Path(item['path']).parent == current
        assert item['sha256'] == hashlib.sha256((current/item['name']).read_bytes()).hexdigest()
    assert json.loads((public/'manifest.json').read_text(encoding='utf-8')) == manifest
    builder.execute(flash=False, port=None, clean=False, environment=ENVIRONMENT)
    assert len(list((public/'builds').iterdir())) == 1
    assert ai_bytes(ai_current, ai_public) == protected
    assert all('--upload-port' not in command and '--target' not in command for command in state['commands'])


@pytest.mark.parametrize('missing', ['bootloader.bin', 'partitions.bin'])
def test_incomplete_s3_bundle_never_publishes_or_replaces_old_board(build_context, missing):
    builder, repo, current, public, state = build_context
    protected = ai_bytes(current, public)
    state['missing'] = missing
    with pytest.raises(RuntimeError, match='incomplete bundle'):
        builder.execute(flash=False, port=None, clean=False, environment=ENVIRONMENT)
    assert not (repo/'firmware/esp32cam/build-xiao-esp32s3-sense').exists()
    assert not (public/'xiao-esp32s3-sense/manifest.json').exists()
    assert ai_bytes(current, public) == protected


def test_failed_s3_build_cannot_delete_the_other_board_or_publish_success(build_context):
    builder, repo, current, public, state = build_context
    protected = ai_bytes(current, public)
    state['failed'] = True
    with pytest.raises(RuntimeError, match='exit code 4'):
        builder.execute(flash=False, port=None, clean=False, environment=ENVIRONMENT)
    assert ai_bytes(current, public) == protected
    assert not (public/'xiao-esp32s3-sense/manifest.json').exists()
    assert not (public/'.build.lock').exists()


@pytest.mark.parametrize('environment', ['../outside', 'esp32cam;whoami', 'esp32s3', '--target=upload', ''])
def test_unknown_environment_rejected_before_filesystem_build_or_ports(build_context, environment):
    builder, *_ = build_context
    with pytest.raises(ValueError):
        builder.execute(flash=False, port=None, clean=False, environment=environment)
    with pytest.raises(ValueError):
        builder.platformio_command(Path('unused'), interpreter=Path('python'), environment=environment)


def test_s3_legacy_build_upload_is_rejected_before_a_port_is_opened(build_context):
    builder, *_ = build_context
    with pytest.raises(RuntimeError, match='ROM'):
        builder.execute(flash=True, port='COM-NEVER-OPEN', clean=False, environment=ENVIRONMENT)


def test_fixed_platformio_environments_and_partition_table_match_flash_contract():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT/'firmware/esp32cam/platformio.ini', encoding='utf-8')
    assert parser['platformio']['default_envs'] == 'esp32cam'
    assert parser['env:esp32cam']['board'] == 'esp32cam'
    assert parser['env:esp32cam']['board_build.partitions'] == 'min_spiffs.csv'
    s3 = parser['env:seeed_xiao_esp32s3']
    assert s3['board'] == ENVIRONMENT and s3['board_build.arduino.memory_type'] == 'qio_opi'
    assert '-DOM_BOARD_XIAO_ESP32S3_SENSE=1' in s3['build_flags']
    csv = ROOT/'firmware/esp32cam'/s3['board_build.partitions']
    entries = [[value.strip() for value in line.split(',')[:5]] for line in csv.read_text().splitlines()
               if line.strip() and not line.startswith('#')]
    assert entries == [
        ['nvs', 'data', 'nvs', '0x9000', '0x5000'],
        ['otadata', 'data', 'ota', '0xe000', '0x2000'],
        ['app0', 'app', 'ota_0', '0x10000', '0x330000'],
        ['app1', 'app', 'ota_1', '0x340000', '0x330000'],
        ['spiffs', 'data', 'spiffs', '0x670000', '0x180000'],
        ['coredump', 'data', 'coredump', '0x7F0000', '0x10000']]
    assert int(entries[-1][3], 16)+int(entries[-1][4], 16) == 8*1024*1024


def test_board_headers_require_exact_target_and_native_usb_without_waiting_for_host():
    header = (ROOT/'firmware/esp32cam/include/board_config.h').read_text()
    assert '#error "Select exactly one ObjectMemory camera board"' in header
    assert '!CONFIG_IDF_TARGET_ESP32S3 || !ARDUINO_USB_CDC_ON_BOOT || !ARDUINO_USB_MODE' in header
    xiao = header.split('#elif defined(OM_BOARD_XIAO_ESP32S3_SENSE)')[1].split('#elif defined(OM_BOARD_AI_THINKER_ESP32CAM)')[0]
    for name, pin in {'PWDN': -1, 'RESET': -1, 'XCLK': 10, 'SIOD': 40, 'SIOC': 39,
                      'Y9': 48, 'Y8': 11, 'Y7': 12, 'Y6': 14, 'Y5': 16, 'Y4': 18,
                      'Y3': 17, 'Y2': 15, 'VSYNC': 38, 'HREF': 47, 'PCLK': 13}.items():
        assert f'{name}_GPIO_NUM = {pin};' in xiao
    main = (ROOT/'firmware/esp32cam/src/main.cpp').read_text()
    assert 'while (!Serial)' not in main and 'while(!Serial)' not in main
    assert 'esp_efuse_mac_get_default(mac)' in main
    assert main.count('doc["board_type"] = OM_BOARD_TYPE;') >= 2


def test_default_ai_board_identifier_matches_existing_usb_api_contract():
    from apps.api.app.firmware import DEFAULT_BOARD_MODEL
    builder = load_script('ai_backward_compatible_board_id', 'scripts/firmware-build.py')
    assert builder.BUILD_PROFILES['esp32cam']['board_type'] == DEFAULT_BOARD_MODEL == 'ai_thinker_esp32cam'
    header = (ROOT/'firmware/esp32cam/include/board_config.h').read_text()
    assert 'OM_BOARD_TYPE = "ai_thinker_esp32cam";' in header


def test_windows_build_wmi_fallback_is_process_local_constant_and_does_not_embed_arguments():
    builder = load_script('xiao_pio_os_query_contract', 'scripts/firmware-build.py')
    prefix = builder.platformio_python_prefix(Path('python'))
    if builder.os.name == 'nt':
        assert prefix[:3] == ['python', '-B', '-c']
        assert "sys.modules['_wmi'] = None" in prefix[3]
        assert "runpy.run_module('platformio', run_name='__main__')" in prefix[3]
        assert 'winreg' not in prefix[3] and 'subprocess' not in prefix[3]
    else:
        assert prefix == ['python', '-m', 'platformio']
    source = Path('directory-with-apostrophe-and-spaces')
    command = builder.platformio_command(source, environment=ENVIRONMENT, interpreter=Path('python'))
    assert command[len(prefix)+2] == str(source)
    assert str(source) not in ''.join(prefix)
