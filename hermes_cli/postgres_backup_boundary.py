"""Keep file archives honest after stores move to DBserver PostgreSQL.

These archives preserve local configuration/files, not a relational database
snapshot. Database recovery uses the separately verified DBserver backup chain.
No database credentials or network destinations are included in the manifest.
"""
from pathlib import Path

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli.postgres_runtime import SCOPES, configuration

MANIFEST = 'postgresql-storage-boundary.json'
NOTICE = ('PostgreSQL data is NOT included in this file archive. '
          'Use the DBserver database backup together with these files for recovery.')


class BackupBoundary:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.profiles = {}
        self._excluded = set()
        self._protected = set()
        self._boards = set()
        homes = [self.home]
        directory = self.home/'profiles'
        if directory.is_dir():
            homes += [p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()]
        for profile in homes:
            token = set_hermes_home_override(profile)
            try:
                selected = [scope for scope in SCOPES if configuration(scope) is not None]
                if not selected:
                    continue
                prefix = profile.relative_to(self.home)
                self.profiles[prefix.as_posix()] = selected
                self._protected.add((prefix/'config.yaml').as_posix())
                # Protect the live credential mapping from old archive copies.
                import yaml
                config = yaml.safe_load((profile/'config.yaml').read_text(encoding='utf-8'))
                secret = Path(config['storage']['postgresql']['config_file']).expanduser()
                secret = secret if secret.is_absolute() else profile/secret
                try:
                    self._protected.add(secret.resolve().relative_to(self.home).as_posix())
                except ValueError:
                    pass  # External credential files aren't archive members.
                for scope in selected:
                    path = (prefix/SCOPES[scope][0]).as_posix()
                    self._excluded.update(path+suffix for suffix in ('', '-wal', '-shm', '-journal'))
                if 'kanban' in selected:
                    self._boards.add((prefix/'kanban/boards').as_posix())
            except Exception as exc:
                # No corrupt-config fallback and no credential/DSN in errors.
                raise RuntimeError('Cannot determine PostgreSQL backup boundary (' + type(exc).__name__ + ')') from None
            finally:
                reset_hermes_home_override(token)

    def excluded(self, relative):
        value = Path(relative).as_posix()
        if value == MANIFEST or value in self._excluded:
            return True
        for prefix in self._boards:
            parts = value.removeprefix(prefix+'/').split('/')
            if value.startswith(prefix+'/') and len(parts) == 2 and parts[-1] in (
                'kanban.db', 'kanban.db-wal', 'kanban.db-shm', 'kanban.db-journal',
            ):
                return True
        return False

    def protected_restore(self, relative):
        try:
            canonical = (self.home/relative).resolve().relative_to(self.home)
        except (ValueError, OSError):
            return True
        return self.excluded(canonical) or canonical.as_posix() in self._protected

    def accepts_manifest(self, manifest):
        """Every archived external store needs its own provisioned target."""
        if not isinstance(manifest, dict) or manifest.get('version') != 1:
            return False
        profiles = manifest.get('postgresql_profiles')
        if not isinstance(profiles, dict) or not profiles:
            return False
        return all(isinstance(stores, list) and all(isinstance(s, str) for s in stores)
                   and set(stores) <= set(self.profiles.get(name, []))
                   and name in self.profiles for name, stores in profiles.items())

    def manifest(self):
        return {'version': 1, 'database_data_included': False,
                'postgresql_profiles': self.profiles,
                'recovery_requirement': NOTICE}
