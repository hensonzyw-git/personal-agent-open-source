"""Synthetic adversarial regressions; no user records or live dependencies."""
from copy import deepcopy

from risk_monitor import afrs
from risk_monitor.daily import collect_breadth
from test_afrs import _companyfacts, _fact


def test_alias_migration_uses_current_annual_fact():
    facts = _companyfacts()
    gaap = facts['facts']['us-gaap']
    gaap['Revenues'] = deepcopy(gaap[afrs.REVENUE_TAGS[0]])
    gaap[afrs.REVENUE_TAGS[0]]['units']['USD'] = [_fact('2021-01-01', '2021-12-31', 1)]
    fund = afrs.extract_fundamentals(facts)
    assert fund['method']['revenue']['end'] == '2025-01-26'
    assert fund['method']['revenue']['tag'] == 'Revenues'


def test_duplicate_disclosure_is_not_previous_year():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap'][afrs.REVENUE_TAGS[0]]['units']['USD']
    rows[0]['val'] = 120_000_000_000
    rows.append(deepcopy(rows[0]))
    fund = afrs.extract_fundamentals(facts)
    assert fund['rev_prior'] == 100_000_000_000


def test_quarter_in_annual_filing_is_not_annual_revenue():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap'][afrs.REVENUE_TAGS[0]]['units']['USD']
    rows.append(_fact('2024-10-28', '2025-01-26', 12))
    assert afrs.extract_fundamentals(facts)['revenue'] == 100_000_000_000


def test_new_quarter_receivables_do_not_mix_with_annual_revenue():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap']['AccountsReceivableNetCurrent']['units']['USD']
    rows.append({'end': '2025-04-27', 'val': 99_000_000_000})
    fund = afrs.extract_fundamentals(facts)
    assert fund['method']['ar']['end'] == fund['method']['revenue']['end']
    assert afrs.compute_values(fund)['company.ar_dso_concentration'] == 10.0


def test_mismatched_capex_and_ocf_are_not_scored():
    facts = _companyfacts()
    facts['facts']['us-gaap'][afrs.CAPEX_TAGS[0]]['units']['USD'] = [
        _fact('2022-01-01', '2022-12-31', 10)
    ]
    fund = afrs.extract_fundamentals(facts)
    assert 'company.capex_ocf_pct' not in afrs.compute_values(fund)
    assert 'cashflow_period_mismatch' in afrs.validate(fund, afrs.compute_values(fund))[1]


def test_conflicting_same_version_facts_fail_closed():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap'][afrs.REVENUE_TAGS[0]]['units']['USD']
    duplicate = deepcopy(rows[0])
    duplicate['val'] += 1
    rows.append(duplicate)
    assert afrs.extract_fundamentals(facts)['revenue'] is None


def test_latest_filed_revision_wins_independent_of_row_order():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap'][afrs.REVENUE_TAGS[0]]['units']['USD']
    rows[0]['filed'] = '2025-02-20'
    amended = dict(rows[0], val=123, filed='2025-03-20')
    rows.insert(0, amended)
    assert afrs.extract_fundamentals(facts)['revenue'] == 123


def test_previous_year_must_be_same_period_not_arbitrary_old_fact():
    facts = _companyfacts()
    rows = facts['facts']['us-gaap'][afrs.REVENUE_TAGS[0]]['units']['USD']
    rows[1].update(start='2020-01-01', end='2020-12-31')
    fund = afrs.extract_fundamentals(facts)
    assert fund['rev_prior'] is None
    assert 'company.ar_dso_concentration' not in afrs.compute_values(fund)


def test_negative_ocf_does_not_produce_green_cashflow_ratios():
    fund = {'capex': 50, 'ocf': -100}
    assert afrs.compute_values(fund) == {}


def test_historical_coverage_does_not_mask_missing_current_closes():
    from datetime import date, timedelta
    series = [((date(2025, 1, 1) + timedelta(days=i)).isoformat(), 100+i) for i in range(230)]
    class Client:
        def collect_closes(self, tickers):
            return {'NEW': series, 'OLD': series[:-1]}, {}
    metrics, meta, _ = collect_breadth(Client(), ['NEW', 'OLD'])
    assert metrics == {}
    assert meta['effective_coverage'] == 0.5
    assert meta['below_threshold'] is True


def test_all_stale_closes_fail_against_explicit_report_date():
    from datetime import date, timedelta
    series = [((date(2025, 1, 1) + timedelta(days=i)).isoformat(), 100+i) for i in range(230)]
    class Client:
        def collect_closes(self, tickers):
            return {'OLD': series}, {}
    metrics, meta, _ = collect_breadth(Client(), ['OLD'], as_of='2026-09-10')
    assert metrics == {}
    assert meta['effective_coverage'] == 0


def test_invalid_financial_period_is_visible_in_daily_quality(tmp_path, monkeypatch):
    from risk_monitor import daily
    from test_daily import _RunFred, _RunTencent
    class Edgar:
        def company_facts(self, cik):
            facts = _companyfacts()
            facts['facts']['us-gaap'][afrs.CAPEX_TAGS[0]]['units']['USD'] = [
                _fact('2022-01-01', '2022-12-31', 10)
            ]
            return facts
    monkeypatch.setattr(daily, 'FredClient', _RunFred)
    monkeypatch.setattr(daily, 'TencentClient', _RunTencent)
    monkeypatch.setattr(daily, 'EdgarClient', Edgar)
    monkeypatch.setattr(daily, 'load_tickers', lambda: ['UP', 'DOWN'])
    result = daily.run(db_path=str(tmp_path / 'risk.db'))
    assert result['quality_status'] == 'data_quality_warning'
