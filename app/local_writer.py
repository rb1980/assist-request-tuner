"""Write Request Tuner's managed files straight into a mounted HA config folder (HA App, or Docker on the HA host)."""
import os
import stat
from pathlib import PurePosixPath
from .parse import now
from .sentence_writer import SentenceWriter, limit


class LocalWriter(SentenceWriter):
    """Same managed files and rules as the SFTP writer: only those two files, no symlinks, atomic replace."""

    def configuration(self):
        error = None
        if not os.path.isdir(self.base):
            error = f'HA config folder {self.base} is not mounted; custom sentence writer disabled'
        elif not os.access(self.base, os.W_OK):
            error = f'HA config folder {self.base} is read-only; custom sentence writer disabled'
        return dict(configured=error is None, state=error or 'Ready (local folder)', host_key_fingerprint=None, mode='local',
                    last_write=self.db.meta('sentence_last_write'), path=self.path,
                    file_size=int(self.db.meta('sentence_file_size')) if self.db.meta('sentence_file_size') is not None else None)

    def check_local(self, name):
        managed, _ = self.managed(name)
        files = (managed, managed + '.tmp')
        for path in [str(p) for p in reversed(PurePosixPath(managed).parents) if str(p) != '/'] + list(files):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('Managed path must not contain symlinks')
            if path not in files and not stat.S_ISDIR(info.st_mode):
                raise ValueError('Managed parent must be a directory')
            if path in files and not stat.S_ISREG(info.st_mode):
                raise ValueError('Managed file must be a regular file')

    def ready(self, name):
        self.managed(name)
        if not self.configuration()['configured']:
            raise ValueError(self.configuration()['state'])
        self.check_local(name)

    async def read(self, name='sentences'):
        async with self.lock:
            self.ready(name)
            path, _ = self.managed(name)
            try:
                with open(path, encoding='utf-8') as stream:
                    text = stream.read(limit(name) + 1)
            except FileNotFoundError:
                text = None
            if text is not None and len(text.encode()) > limit(name):
                raise ValueError('Managed file is too large')
            if name == 'sentences':
                self.db.meta('sentence_file_size', len(text.encode()) if text is not None else 0)
            return text

    async def write(self, text, name='sentences'):
        path, directories = self.writable(name)
        temp = path + '.tmp'
        if text is not None and (not isinstance(text, str) or len(text.encode()) > 2_000_000):
            raise ValueError('Invalid managed file content')
        async with self.lock:
            self.ready(name)
            if text is None:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            else:
                for directory in directories:
                    try:
                        os.mkdir(directory, 0o755)
                    except FileExistsError:
                        pass
                self.check_local(name)
                try:
                    os.remove(temp)
                except FileNotFoundError:
                    pass
                # O_EXCL never follows a raced symlink; os.replace is atomic on one filesystem.
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(text)
                os.replace(temp, path)
            self.db.meta('sentence_last_write' if name == 'sentences' else 'blueprint_last_write', now())
            if name == 'sentences':
                self.db.meta('sentence_file_size', len(text.encode()) if text is not None else 0)


class DisabledWriter(SentenceWriter):
    def configuration(self):
        return dict(configured=False, state='HA_CONFIG_ACCESS is none: fixes needing a file (custom sentences, blueprint) can be drafted but not applied',
                    host_key_fingerprint=None, mode='none', last_write=None, file_size=None, path=self.path)


def make_writer(db, settings):
    mode = settings.ha_config_access
    if mode == 'local':
        return LocalWriter(db, settings)
    if mode == 'none':
        return DisabledWriter(db, settings)
    if mode != 'sftp':
        raise ValueError('HA_CONFIG_ACCESS must be local, sftp or none')
    return SentenceWriter(db, settings)
