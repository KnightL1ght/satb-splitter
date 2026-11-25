"""Batch 11b: MuseScore child env without headless Qt overrides + fail-fast watchdog.

No real MuseScore: subprocess.Popen is replaced by a fake process.
"""
import inspect
import io
import os
import time
import zipfile

import pytest

import satb_splitter.export.mscz as mz


class FakeProc:
    """Minimal Popen stand-in. mode: ok | fail | hang | qt."""
    instances = []

    def __init__(self, cmd, mode='ok', **kw):
        self.cmd, self.mode, self.kw = cmd, mode, kw
        self.pid = 0          # pid 0 -> no dialog lookup, never a real process
        self.killed = False
        self.returncode = None
        err = b''
        if mode == 'qt':
            err = b'qt.qpa.plugin: Could not find the Qt platform plugin "offscreen" in ""\n'
        self.stdout, self.stderr = io.BytesIO(b''), io.BytesIO(err)
        if mode == 'ok':
            with zipfile.ZipFile(cmd[2], 'w') as z:
                z.writestr('score.mscx', '<museScore/>')
            self.returncode = 0
        elif mode == 'fail':
            self.returncode = 1
        FakeProc.instances.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake(monkeypatch, tmp_path):
    exe = tmp_path / 'MuseScore4.exe'
    exe.write_text('')
    monkeypatch.setenv('SATB_MSCZ', '1')
    monkeypatch.setenv('SATB_MUSESCORE', str(exe))
    monkeypatch.delenv('SATB_MSCZ_TIMEOUT', raising=False)
    monkeypatch.delenv('SATB_MSCZ_STALL', raising=False)
    monkeypatch.setattr(mz, '_POLL_S', 0.02)
    killed = []
    monkeypatch.setattr(mz, '_kill_tree', lambda proc: (killed.append(proc), proc.kill()))
    FakeProc.instances = []
    state = {'mode': 'ok', 'killed': killed}

    def popen(cmd, **kw):
        return FakeProc(cmd, mode=state['mode'], **kw)
    monkeypatch.setattr(mz.subprocess, 'Popen', popen)
    src = tmp_path / 'song_SATB.musicxml'
    src.write_text('<score-partwise/>')
    state['src'] = src
    return state


def test_child_env_strips_headless_qt_vars_parent_unchanged(fake, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', r'C:\x\platforms')
    monkeypatch.setenv('QT_PLUGIN_PATH', r'C:\x\plugins')
    monkeypatch.setenv('QT_QPA_PLATFORMTHEME', 'gtk3')
    monkeypatch.setenv('QT_DEBUG_PLUGINS', '1')
    monkeypatch.setenv('KEEP_ME', 'yes')
    before = dict(os.environ)
    res = mz.export_mscz(fake['src'])
    assert res.ok
    child = FakeProc.instances[0].kw['env']
    for var in ('QT_QPA_PLATFORM', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_PLUGIN_PATH',
                'QT_QPA_PLATFORMTHEME', 'QT_DEBUG_PLUGINS'):
        assert var not in child
        assert os.environ[var] == before[var]
    assert child['KEEP_ME'] == 'yes' and 'SATB_MUSESCORE' in child
    assert dict(os.environ) == before


def test_musescore_child_env_helper_is_a_copy():
    base = {'QT_QPA_PLATFORM': 'offscreen', 'qt_plugin_path': 'x', 'PATH': 'p'}
    env, removed = mz.musescore_child_env(base)
    assert env == {'PATH': 'p'} and removed == ['QT_QPA_PLATFORM', 'qt_plugin_path']
    assert base['QT_QPA_PLATFORM'] == 'offscreen'


def test_success_path_unchanged(fake):
    res = fake['src'].with_suffix('.mscz')
    out = mz.export_mscz(fake['src'])
    assert out.path == res and res.is_file()
    assert out.message == 'MuseScore file written: song_SATB.mscz (via MuseScore4.exe)'
    assert not fake['killed']
    assert not list(fake['src'].parent.glob('*.tmp-export.mscz'))
    assert res.with_name(res.name + '.splitter.json').is_file()


def test_default_timeout_is_60(fake, monkeypatch):
    assert inspect.signature(mz.export_mscz).parameters['timeout_s'].default == 60
    assert mz.DEFAULT_TIMEOUT_S == 60
    seen = {}

    def run(cmd, tmp, env, timeout_s, stall_s, kw):
        seen['t'], seen['s'] = timeout_s, stall_s
        return 1, '', '', None
    monkeypatch.setattr(mz, '_run_musescore', run)
    mz.export_mscz(fake['src'])
    assert seen == {'t': 60, 's': mz.DEFAULT_STALL_S}
    monkeypatch.setenv('SATB_MSCZ_TIMEOUT', '15')
    mz.export_mscz(fake['src'])
    assert seen['t'] == 15


def test_hanging_process_killed_at_timeout(fake, monkeypatch):
    fake['mode'] = 'hang'
    monkeypatch.setenv('SATB_MSCZ_TIMEOUT', '0.3')
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: None)  # no CPU info -> timeout only
    t = time.monotonic()
    res = mz.export_mscz(fake['src'])
    assert time.monotonic() - t < 5
    assert res.path is None and 'timed out after 0.3s' in res.message and 'killed' in res.message
    assert fake['killed'] and not list(fake['src'].parent.glob('*.mscz'))


def test_idle_process_killed_at_stall_window(fake, monkeypatch):
    fake['mode'] = 'hang'
    monkeypatch.setenv('SATB_MSCZ_STALL', '0.3')
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: 0.06)  # CPU flat = waiting on a dialog
    t = time.monotonic()
    res = mz.export_mscz(fake['src'])
    assert time.monotonic() - t < 5
    assert res.path is None and 'stalled' in res.message and fake['killed']


def test_busy_process_not_killed_by_stall_window(fake, monkeypatch):
    fake['mode'] = 'hang'
    monkeypatch.setenv('SATB_MSCZ_STALL', '0.2')
    monkeypatch.setenv('SATB_MSCZ_TIMEOUT', '0.6')
    ticks = iter(range(10 ** 6))
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: next(ticks) * 0.1)  # CPU keeps rising
    res = mz.export_mscz(fake['src'])
    assert 'timed out after 0.6s' in res.message and 'stalled' not in res.message


def test_qt_plugin_stderr_detected_early(fake, monkeypatch, tmp_path):
    fake['mode'] = 'qt'
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: None)
    (tmp_path / 'song_SATB.tmp-export.mscz').write_bytes(b'partial')
    t = time.monotonic()
    res = mz.export_mscz(fake['src'])
    assert time.monotonic() - t < 3
    assert res.path is None and 'Qt platform-plugin error' in res.message
    assert 'Could not find the Qt platform plugin' in res.message
    assert fake['killed'] and not (tmp_path / 'song_SATB.tmp-export.mscz').exists()


def test_dialog_detected_early(fake, monkeypatch):
    fake['mode'] = 'hang'
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: None)
    monkeypatch.setattr(mz, '_dialog_text', lambda pid: 'MuseScore4 | This application failed to start')
    res = mz.export_mscz(fake['src'])
    assert res.path is None and 'showed a dialog' in res.message and fake['killed']


def test_failure_message_reaches_review_and_log(fake, monkeypatch, tmp_path):
    from satb_splitter.export.merge import split_and_merge_mxl_files
    from pathlib import Path
    fake['mode'] = 'qt'
    monkeypatch.setattr(mz, '_process_cpu_seconds', lambda proc: None)
    p1 = Path(__file__).parent / 'fixtures' / 'batch11' / 'b11_easy_page_01.mxl'
    written, log, combined = split_and_merge_mxl_files([p1], tmp_path / 'out', 'song')
    assert combined.mscz_path is None and 'Qt platform-plugin error' in combined.mscz_message
    assert any(ln.startswith('WARNING: .mscz not written') for ln in log)
    review = (tmp_path / 'out' / 'song_REVIEW.txt').read_text(encoding='utf-8')
    assert 'MuseScore: .mscz not written: MuseScore reported a Qt platform-plugin error' in review
