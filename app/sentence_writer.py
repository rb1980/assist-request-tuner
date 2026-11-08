"""SFTP access confined to Request Tuner's managed files."""
import asyncio
from . import names
import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
import asyncssh
from .ha import HAError
from .parse import now


class HostKeyClient(asyncssh.SSHClient):
    def __init__(self, expected):
        self.expected, self.key = expected, None

    def validate_host_public_key(self, host, addr, port, key):
        if self.expected and key != asyncssh.import_public_key(self.expected):
            return False
        self.key = key
        return True


def limit(name):
    """Largest file Request Tuner reads: its own files are small; HA's config entries grow with the number of integrations."""
    return 20_000_000 if name == 'config_entries' else 2_000_000


class SentenceWriter:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        base = PurePosixPath(settings.ha_config_dir)
        if not base.is_absolute() or '..' in base.parts or str(base) == '/':
            raise ValueError('HA_CONFIG_DIR must be an absolute config directory without traversal')
        self.base = str(base)
        self.path = f'{self.base}/{names.SENTENCE_FILE}'
        self.directory = self.path.rsplit('/', 1)[0]
        self.temp = self.path + '.tmp'
        self.lock = asyncio.Lock()

    def configuration(self):
        path = Path(self.settings.ha_ssh_key_file)
        error = None
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                error = 'SSH key must be a private file with mode 600 or 400'
        except OSError:
            error = 'SSH key file missing; custom sentence writer disabled'
        return dict(configured=error is None, state=error or self.db.meta('sentence_writer_state') or 'Ready', mode='sftp',
                    host_key_fingerprint=self.db.meta('sentence_host_fingerprint'),
                    last_write=self.db.meta('sentence_last_write'), file_size=int(self.db.meta('sentence_file_size')) if self.db.meta('sentence_file_size') is not None else None, path=self.path)

    def forget_host_key(self):
        self.db.meta('sentence_host_key', '')
        self.db.meta('sentence_host_fingerprint', '')
        self.db.meta('sentence_writer_state', 'Ready')

    def managed(self, name):
        if name == 'sentences':
            return self.path, (self.base + '/custom_sentences', self.directory)
        if name == 'blueprint':
            directory = f'{self.base}/{names.BLUEPRINT_DIR}'
            return f'{directory}/{names.BLUEPRINT_FILE}', (directory,)
        if name == 'config_entries':
            # Read only: HA's integration settings, for the Local OpenAI LLM servers (ha_llm.py). Never written.
            return self.base + '/.storage/core.config_entries', ()
        raise ValueError('Unknown managed file')

    def writable(self, name):
        if name == 'config_entries':
            raise ValueError("Home Assistant's config entries are read only")
        return self.managed(name)

    @asynccontextmanager
    async def session(self, name='sentences'):
        self.managed(name)
        if not self.configuration()['configured']:
            raise ValueError(self.configuration()['state'])
        settings = self.settings
        identity = f'{settings.ha_ssh_host}:{settings.ha_ssh_port}'
        trusted = json.loads(self.db.meta('sentence_host_key') or '{}')
        # Trust on first use, per address. A new address (an IP that changed, homeassistant.local vs an IP, a new port)
        # trusts again, because HA is often reached by several names. The same address presenting a different key is
        # refused: that is what an impostor looks like. An explicit host key in the config always wins.
        expected = settings.ha_ssh_host_key or (trusted.get('key') if trusted.get('host') == identity else None)
        client = HostKeyClient(expected)
        try:
            async with asyncio.timeout(20):
                async with asyncssh.connect(settings.ha_ssh_host, port=settings.ha_ssh_port, username=settings.ha_ssh_user,
                    client_keys=[settings.ha_ssh_key_file], agent_path=None, config=None, known_hosts=(),
                    client_factory=lambda: client, password_auth=False, kbdint_auth=False) as connection:
                    key = connection.get_server_host_key()
                    self.db.meta('sentence_host_key', json.dumps({'host': identity, 'key': key.export_public_key().decode().strip()}))
                    self.db.meta('sentence_host_fingerprint', key.get_fingerprint())
                    async with connection.start_sftp_client() as sftp:
                        await self.check_paths(sftp, name)
                        yield sftp
                    self.db.meta('sentence_writer_state', 'Ready')
        except asyncssh.HostKeyNotVerifiable:
            message = ("SFTP writer failed: Home Assistant's SSH host key is not the one Request Tuner saved for "
                       f"{identity}. If you reinstalled the SSH App or Home Assistant, use “Forget saved host key” in Settings.")
            self.db.meta('sentence_writer_state', message)
            raise HAError(message) from None
        except (asyncssh.Error, asyncssh.KeyImportError, OSError, TimeoutError) as exc:
            # Never expose private key content or parser diagnostics.
            message = 'SFTP writer failed: ' + type(exc).__name__
            self.db.meta('sentence_writer_state', message)
            raise HAError(message) from None

    async def check_paths(self, sftp, name='sentences'):
        managed, _ = self.managed(name)
        files = (managed, managed + '.tmp')
        paths = [str(p) for p in reversed(PurePosixPath(managed).parents) if str(p) != '/'] + list(files)
        for path in paths:
            try:
                info = await sftp.lstat(path)
            except asyncssh.SFTPNoSuchFile:
                continue
            if info.permissions is None or stat.S_ISLNK(info.permissions):
                raise ValueError('SFTP managed path must not contain symlinks')
            if path not in files and not stat.S_ISDIR(info.permissions):
                raise ValueError('SFTP managed parent must be a directory')
            if path in files and not stat.S_ISREG(info.permissions):
                raise ValueError('SFTP managed file must be a regular file')

    async def read(self, name='sentences'):
        async with self.lock, self.session(name) as sftp:
            path, _ = self.managed(name)
            try:
                async with sftp.open(path, 'r') as stream:
                    text = await stream.read(limit(name) + 1)
                if len(text.encode()) > limit(name):
                    raise ValueError('Managed sentence file is too large')
            except asyncssh.SFTPNoSuchFile:
                text = None
            if name == 'sentences':
                self.db.meta('sentence_file_size', len(text.encode()) if text is not None else 0)
            return text

    async def write(self, text, name='sentences'):
        self.writable(name)
        if text is not None and (not isinstance(text, str) or len(text.encode()) > 2_000_000):
            raise ValueError('Invalid managed file content')
        async with self.lock, self.session(name) as sftp:
            path, directories = self.managed(name)
            temp = path + '.tmp'
            if text is None:
                try:
                    await sftp.remove(path)
                except asyncssh.SFTPNoSuchFile:
                    pass
            else:
                # Only the selected managed parent directories may be created.
                for directory in directories:
                    try:
                        await sftp.mkdir(directory)
                    except asyncssh.SFTPFileAlreadyExists:
                        pass
                    except asyncssh.SFTPFailure:
                        if not await sftp.isdir(directory):
                            raise
                await self.check_paths(sftp, name)
                try:
                    await sftp.remove(temp)
                except asyncssh.SFTPNoSuchFile:
                    pass
                # Exclusive creation cannot follow a raced symlink or truncate an old hard link.
                async with sftp.open(temp, 'x') as stream:
                    await stream.write(text)
                await sftp.posix_rename(temp, path)
            self.db.meta('sentence_last_write' if name == 'sentences' else 'blueprint_last_write', now())
            if name == 'sentences':
                self.db.meta('sentence_file_size', len(text.encode()) if text is not None else 0)
