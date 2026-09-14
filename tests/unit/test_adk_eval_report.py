import json
from pathlib import Path
from personal_agent.runtime.eval_report import summarize


def test_missing_trials_never_pass_gate():
    data=json.loads(Path('evals/adk_model_led_v0.1.json').read_text())
    result=summarize(data,[])
    assert result['missing']==480 and not result['numeric_thresholds_met'] and not result['activation_gate_passed']
