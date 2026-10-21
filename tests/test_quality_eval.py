import copy
import json
from pathlib import Path

import pytest

from research.quality_eval import score


def corpus():
    return json.loads((Path(__file__).parent/'fixtures/research_quality/corpus.json').read_text())


def test_draft_is_never_self_approved():
    data = corpus()
    result = score(data, [{'id': row['id'], 'label': row['proposed_label']} for row in data['cases']])
    assert result['status'] == 'pending_human_review'
    assert result['human_reviewed'] == result['scored'] == result['model_calls'] == 0
    assert result['accuracy'] is None and len(result['pending_human_review']) == 12


def test_confusion_preserves_false_positives_negatives_and_unknown():
    data = corpus()
    for row in data['cases']:
        row.update(human_label=row['proposed_label'], reviewer='test fixture reviewer', reviewed_at='2026-10-16T00:00:00Z')
    predictions = [{'id': row['id'], 'label': row['proposed_label']} for row in data['cases']]
    predictions[0]['label'] = 'unsupported'
    predictions[1]['label'] = 'supported'
    predictions[2]['label'] = 'undetermined'
    result = score(data, predictions)
    assert result['false_negatives'] == [predictions[0]['id']]
    assert result['false_positives'] == [predictions[1]['id']]
    assert predictions[2]['id'] in result['abstentions']
    assert result['accuracy'] == 9/12
    assert score(data, predictions[:-1])['status'] == 'incomplete_predictions'


def test_changed_source_or_unattributed_review_rejected():
    data = corpus()
    bad = copy.deepcopy(data)
    bad['sources'][0]['excerpt'] += ' altered'
    with pytest.raises(ValueError):
        score(bad, [])
    data['cases'][0]['human_label'] = 'supported'
    with pytest.raises(ValueError):
        score(data, [])
    with pytest.raises(ValueError):
        score(corpus(), [{'id':'missing','label':'supported'}])
