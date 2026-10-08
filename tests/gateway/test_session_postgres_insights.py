"""Run existing analytics contracts against real PostgreSQL session data."""
import importlib.util
import inspect
from pathlib import Path

import pytest

_core_spec = importlib.util.spec_from_file_location(
    'native_insights_fixtures', Path(__file__).with_name('test_session_postgres_core.py')
)
_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_core)
database, profile = _core.database, _core.profile


_spec = importlib.util.spec_from_file_location(
    'legacy_insights_contracts', Path(__file__).parents[1] / 'agent/test_insights.py'
)
_contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contracts)

# These three assert SQLite-specific catalog/EXPLAIN/index-hint behavior.
# Every report, cost, source filter, tool, skill and rendering contract runs.
_sqlite_plan_tests = {
    'test_assistant_call_queries_use_partial_index_without_analyze',
    'test_assistant_call_rows_invariant_to_index_selection',
    'test_missing_index_falls_back_to_unpinned_queries',
}
_cases = []
for name, cls in vars(_contracts).items():
    if name.startswith('Test') and inspect.isclass(cls):
        for method_name, method in vars(cls).items():
            if method_name.startswith('test_') and method_name not in _sqlite_plan_tests:
                if {'db', 'populated_db'} & set(inspect.signature(method).parameters):
                    _cases.append((cls, method_name))


@pytest.mark.parametrize('cls,method_name', _cases, ids=[m for _, m in _cases])
def test_native_insights_contract(database, cls, method_name):
    method = getattr(cls(), method_name)
    parameters = inspect.signature(method).parameters
    if 'populated_db' in parameters:
        value = _contracts.populated_db.__wrapped__(database)
        method(populated_db=value)
    else:
        method(db=database)
    assert not database.db_path.exists()


def test_native_insights_unavailable_usage_is_not_reported_as_zero(database):
    from agent.insights import InsightsEngine
    import psycopg
    database.create_session('usage-error', source='api')
    engine = InsightsEngine(database)
    database._conn.execute('ALTER TABLE session_model_usage RENAME TO unavailable_usage')
    with pytest.raises(psycopg.errors.UndefinedTable):
        engine.generate()
