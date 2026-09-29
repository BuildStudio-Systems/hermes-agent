import json
from concurrent.futures import ThreadPoolExecutor
from gateway.there_chat_scope import device_capability_scope, current_device_capability, ThereChatScope
from plugins.there_devices import call


def test_actual_plugin_discovery_with_profile_settings(tmp_path, monkeypatch):
    import yaml
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text(yaml.safe_dump({'plugins': {
        'enabled': ['there-devices'],
        'entries': {'there-devices': {'settings': {'enabled': True}}}}}))
    manager = PluginManager()
    manager.discover_and_load()
    entry = registry.get_entry('there_devices', scope=manager.scope_key)
    assert entry is not None
    assert json.loads(entry.handler({'action':'list'}))['error'] == 'device_management_requires_registered_owner_chat'


def test_unscoped_and_approval_calls_never_connect():
    assert json.loads(call({'action':'list'}))['error'] == 'device_management_requires_registered_owner_chat'
    with device_capability_scope('synthetic'):
        for action in ('approve','cancel','execute','shell'):
            assert json.loads(call({'action':action}))['error'] == 'action_not_authorized'
    assert current_device_capability() == ''


def test_capability_not_in_repr_or_session_identity():
    a = ThereChatScope('owner','chat','private-capability')
    b = ThereChatScope('owner','chat','different-capability')
    assert a == b and a.session_id == b.session_id
    assert 'private-capability' not in repr(a)


def test_concurrent_scope_and_exception_cleanup():
    def worker(value):
        try:
            with device_capability_scope(value):
                assert current_device_capability() == value
                raise RuntimeError('synthetic')
        except RuntimeError:
            return current_device_capability()
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(worker, ['a','b','c','d'])) == ['']*4
