"""Offline manual-review scoring. Never invokes a model or approves draft labels."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

LABELS = frozenset({'supported', 'unsupported', 'undetermined'})


def score(corpus: dict[str, Any], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(corpus, dict) or not isinstance(predictions, list):
        raise ValueError('invalid evaluation input')
    cases = corpus.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('invalid case count')
    if not all(isinstance(case, dict) and isinstance(case.get('id'), str) for case in cases):
        raise ValueError('invalid case')
    identities = [case['id'] for case in cases]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate case id')
    sources = corpus.get('sources', [])
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        raise ValueError('invalid sources')
    source_ids = {source['id'] for source in sources}
    if len(source_ids) != len(sources):
        raise ValueError('duplicate source')
    for source in sources:
        if hashlib.sha256(source['excerpt'].encode()).hexdigest() != source['excerpt_sha256']:
            raise ValueError('source excerpt changed; review again')
    results: dict[str, str] = {}
    if len(predictions) > len(cases):
        raise ValueError('too many predictions')
    for row in predictions:
        if not isinstance(row, dict) or set(row) != {'id', 'label'} or row['id'] not in identities or row['label'] not in LABELS or row['id'] in results:
            raise ValueError('invalid prediction')
        results[row['id']] = row['label']
    matrix = {truth: {prediction: 0 for prediction in sorted(LABELS)} for truth in sorted(LABELS)}
    pending, missing, false_positive, false_negative, abstained = [], [], [], [], []
    reviewed = 0
    for case in cases:
        identity = case['id']
        if case['source_id'] not in source_ids:
            raise ValueError('unknown source')
        truth = case.get('human_label')
        if truth is None:
            pending.append(identity)
            continue
        if truth not in LABELS or not isinstance(case.get('reviewer'), str) or not case['reviewer'].strip():
            raise ValueError('human review attribution required')
        stamp = datetime.fromisoformat(case.get('reviewed_at') or '')
        if stamp.tzinfo is None:
            raise ValueError('review time must include timezone')
        reviewed += 1
        prediction = results.get(identity)
        if prediction is None:
            missing.append(identity)
            continue
        matrix[truth][prediction] += 1
        if prediction == 'undetermined':
            abstained.append(identity)
        if truth == 'unsupported' and prediction == 'supported':
            false_positive.append(identity)
        if truth == 'supported' and prediction == 'unsupported':
            false_negative.append(identity)
    scored = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[label][label] for label in LABELS)
    return {'status': 'pending_human_review' if pending else 'incomplete_predictions' if missing else 'scored',
            'human_reviewed': reviewed, 'scored': scored, 'accuracy': correct/scored if scored else None,
            'pending_human_review': pending, 'missing_predictions': missing, 'confusion_matrix': matrix,
            'false_positives': false_positive, 'false_negatives': false_negative, 'abstentions': abstained,
            'model_calls': 0, 'cost_usd': '0.00', 'scope': 'manual labels and supplied predictions only; not provider or ASR acceptance'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('predictions', type=Path, nargs='?')
    args = parser.parse_args()
    try:
        if args.corpus.stat().st_size > 256_000 or (args.predictions and args.predictions.stat().st_size > 64_000):
            raise ValueError('input too large')
        corpus = json.loads(args.corpus.read_text())
        predictions = json.loads(args.predictions.read_text()) if args.predictions else []
        print(json.dumps(score(corpus, predictions), ensure_ascii=False, indent=2))
    except (ValueError, TypeError, KeyError, OSError):
        parser.exit(2, 'Invalid quality-evaluation input; check identities, source hashes and human review fields.\n')


if __name__ == '__main__':
    main()
