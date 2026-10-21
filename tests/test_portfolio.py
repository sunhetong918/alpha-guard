from datetime import date
import sqlite3

import pytest

from research.portfolio import FIELDS, PortfolioConflictError, PortfolioStore, execute_portfolio, parse_positions

HEADER = ','.join(FIELDS) + '\n'


def draft(rows='US,AAPL,10,100,USD\n', **changes):
    return {'account': '测试账户', 'snapshot_date': date.today().isoformat(),
            'source_label': '/private/user/holdings.csv', 'csv_text': HEADER + rows,
            'mapping': dict(zip(FIELDS, FIELDS)), 'full_snapshot': True,
            'confirmed_instruments': ['US:AAPL', 'HK:00700'], **changes}


def save(store, fields):
    preview = store.preview(fields)
    return store.commit({**fields, 'expected_revision': preview['revision'], 'preview_token': preview['preview_token']})


def test_preview_replace_duplicate_reopen_and_undo(tmp_path):
    path = tmp_path / 'state.db'
    with PortfolioStore(path) as store:
        fields = draft()
        preview = store.preview(fields)
        assert preview['can_commit'] and preview['changes'][0]['change'] == 'added'
        assert store.view(fields['account'])['positions'] == []
        first = save(store, fields)
        assert first['source_label'] == 'holdings.csv'
        duplicate = save(store, fields)
        assert duplicate['status'] == 'duplicate' and duplicate['revision'] == 1
        replacement = draft('HK,00700,100,,\nUS,AAPL,0,,\n')
        assert {v['change'] for v in store.preview(replacement)['changes']} == {'added', 'removed'}
        second = save(store, replacement)
        assert second['positions'] == [{'instrument': 'HK:00700', 'quantity': '100', 'unit_cost': None, 'cost_currency': None}]
    with PortfolioStore(path) as store:
        restored = store.undo({'account': fields['account'], 'expected_revision': second['revision']})
        assert restored['positions'] == first['positions']
        assert restored['revision'] == 3
        empty = store.undo({'account': fields['account'], 'expected_revision': 3})
        assert empty['positions'] == [] and not empty['can_undo']
        assert store._connection.execute('SELECT count(*) FROM research_portfolio_snapshots').fetchone()[0] == 2


@pytest.mark.parametrize('row,code', [
    ('US,AAPL,-1,,', 'quantity'), ('US,AAPL,NaN,,', 'quantity'),
    ('US,AAPL,1e3,,', 'quantity'), ('US,AAPL,1.000000001,,', 'quantity'),
    ('US,AAPL,1,10,', 'unit_cost_or_currency'), ('US,AAPL,1,10,XYZ', 'unit_cost_or_currency'),
    ('WRONG,AAPL,1,,', 'instrument'), ('US,AAPL,1', 'column_count'),
    ('US,AAPL,1,,,extra', 'column_count'),
])
def test_bad_rows_are_located_and_block_atomic_commit(tmp_path, row, code):
    with PortfolioStore(tmp_path / 'state.db') as store:
        first = save(store, draft())
        fields = draft(row + '\n')
        preview = store.preview(fields)
        assert preview['errors'][0]['line'] == 2
        assert preview['errors'][0]['code'] == code
        with pytest.raises(ValueError):
            save(store, fields)
        assert store.view(fields['account']) == {k: v for k, v in first.items() if k != 'status'}


def test_unknown_confirmation_duplicate_identity_and_mapping(tmp_path):
    with PortfolioStore(tmp_path / 'state.db') as store:
        fields = draft(confirmed_instruments=[])
        assert store.preview(fields)['unconfirmed'] == [{'line': 2, 'instrument': 'US:AAPL'}]
        with pytest.raises(ValueError):
            save(store, fields)
        assert save(store, draft())['positions'][0]['quantity'] == '10'
        assert store.preview(draft('US,AAPL,1,,\nUS,aapl,2,,\n'))['errors'][0]['code'] == 'duplicate_instrument'
    rows, errors = parse_positions('市场,代码,股数\nHK,700,10.50\n', dict(market='市场', ticker='代码', quantity='股数', unit_cost='', cost_currency=''))
    assert not errors and rows[0]['instrument'] == 'HK:00700' and rows[0]['quantity'] == '10.5'


def test_conflict_tamper_and_sql_failure_preserve_current(tmp_path):
    with PortfolioStore(tmp_path / 'state.db') as store:
        fields = draft()
        preview = store.preview(fields)
        save(store, fields)
        with pytest.raises(PortfolioConflictError):
            store.commit({**fields, 'expected_revision': 0, 'preview_token': preview['preview_token']})
        fresh = store.preview(draft('US,AAPL,20,,\n'))
        with pytest.raises(PortfolioConflictError):
            store.commit({**draft('US,AAPL,30,,\n'), 'expected_revision': 1, 'preview_token': fresh['preview_token']})
        store._connection.execute("CREATE TRIGGER fail_pointer BEFORE UPDATE ON research_portfolio_accounts BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            save(store, draft('US,AAPL,20,,\n'))
        assert store.view(fields['account'])['positions'][0]['quantity'] == '10'
        assert store._connection.execute('SELECT count(*) FROM research_portfolio_snapshots').fetchone()[0] == 1


def test_empty_snapshot_accounts_and_strict_boundaries(tmp_path):
    path = tmp_path / 'state.db'
    with PortfolioStore(path) as store:
        save(store, draft())
        assert save(store, draft(''))['positions'] == []
        assert store.view('other')['revision'] == 0
        for fields in (draft(full_snapshot=False), draft(snapshot_date='2999-01-01'), draft(csv_text='x'*256001)):
            with pytest.raises(ValueError):
                store.preview(fields)
    with pytest.raises(ValueError):
        execute_portfolio(path, 'portfolio_get', {'account': 'x', 'extra': True})
