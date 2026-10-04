"""Read-only identity diagnostics, separate from the model result adoption gate.

The daily check covers watchlist ``research_analysis`` calls only. It reads a
small metadata file at the call's fixed location, never prompts or model logs.
"""
import json
import os
import re
import stat

VERSION = 'model-identity-v1'
MAX_METADATA_BYTES = 32768
FIELDS = ('requested_model', 'requested_effort', 'actual_model', 'actual_effort')
TIME_FIELDS = ('started_at', 'finished_at')


def identity(meta):
    """Compare only explicitly pinned fields; missing observations never pass."""
    meta = meta if isinstance(meta, dict) else {}
    values = {key: value if isinstance(value := meta.get(key), str)
              and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value) else None for key in FIELDS}
    invalid = [key for key in FIELDS if meta.get(key) is not None and values[key] is None]
    requested = {'model': values['requested_model'], 'effort': values['requested_effort']}
    actual = {'model': values['actual_model'], 'effort': values['actual_effort']}
    pinned = [key for key in requested if requested[key]]
    missing = [key for key in pinned if not actual[key]]
    mismatched = [key for key in pinned if actual[key] and actual[key] != requested[key]]
    status = 'MISMATCH' if mismatched else 'UNKNOWN' if missing or invalid else 'MATCH' if pinned else 'UNPINNED'
    return {'version': VERSION, 'status': status, 'code': 'MODEL_IDENTITY_' + status,
            'requested': requested, 'actual': actual, 'missing_fields': missing,
            'mismatched_fields': mismatched, 'invalid_fields': invalid}


def metadata(store, run_id):
    """Open a bounded regular meta.json using no-follow directory descriptors."""
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        return None, 'MODEL_METADATA_INVALID_REFERENCE'
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(store.root, directory_flags)
        descriptors.append(fd)
        for component in ('workflow', 'research', run_id):
            fd = os.open(component, directory_flags, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open('meta.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, 'MODEL_METADATA_NOT_REGULAR'
        if info.st_size > MAX_METADATA_BYTES:
            return None, 'MODEL_METADATA_TOO_LARGE'
        chunks, remaining = [], MAX_METADATA_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk);remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > MAX_METADATA_BYTES:
            return None, 'MODEL_METADATA_TOO_LARGE'
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None, 'MODEL_METADATA_INVALID'
        # No hashes, file paths, prompts, result bodies or arbitrary metadata survive.
        return {key: value.get(key) for key in FIELDS + TIME_FIELDS}, None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None, 'MODEL_METADATA_UNAVAILABLE'
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def timing(meta, ready):
    """A later or undated metadata file cannot certify an earlier review."""
    from .storage import normalize_time
    if not all(isinstance(meta.get(key), str) and meta[key] for key in TIME_FIELDS):
        return 'MODEL_METADATA_TIME_UNKNOWN'
    try:
        started, finished = (normalize_time(meta[key]) for key in TIME_FIELDS)
    except (TypeError, ValueError):
        return 'MODEL_METADATA_TIME_INVALID'
    if started > finished:
        return 'MODEL_METADATA_TIME_INVALID'
    if finished > normalize_time(ready):
        return 'MODEL_METADATA_AFTER_CUTOFF'
    return None


def check(store, start, end, ready=None):
    """A complete CHECK_MODEL_IDENTITY result for the original watchlist scope."""
    from .storage import normalize_time
    ready = normalize_time(ready or end)
    attempts = store.db.execute('''SELECT id,status,detail,symbol,run_id FROM data_attempts
        WHERE source='research_analysis' AND checked_at>=? AND checked_at<? ORDER BY checked_at,id''',
        (normalize_time(start), normalize_time(end)))
    checked = failed = missing = reused = legacy = 0
    failure_examples, missing_examples = [], []
    for attempt in attempts:
        if not attempt['run_id'] and attempt['status'] == 'OK':
            # renew() writes OK without a new call id. It is not a new verified call.
            reused += 1
            continue
        meta, error = metadata(store, attempt['run_id'])
        time_error = timing(meta, ready) if meta else None
        verdict = identity(meta)
        item = {'route': 'watchlist', 'attempt_id': attempt['id'], 'symbol': attempt['symbol'],
                'run_id': attempt['run_id'] if isinstance(attempt['run_id'], str)
                and re.fullmatch(r'[0-9a-f]{32}', attempt['run_id']) else None}
        if not time_error and verdict['status'] in ('MATCH', 'MISMATCH'):
            checked += 1
            if verdict['status'] == 'MISMATCH':
                failed += 1
                if len(failure_examples) < 5:
                    labels = {'model': '模型', 'effort': '推理配置'}
                    detail = '；'.join(f"{labels[key]}要求{verdict['requested'][key]}，实际{verdict['actual'][key]}"
                                      for key in verdict['mismatched_fields'])
                    failure_examples.append({**item, **verdict, 'evidence': 'CALL_METADATA', 'detail': detail})
        elif time_error not in ('MODEL_METADATA_AFTER_CUTOFF', 'MODEL_METADATA_TIME_INVALID') \
                and attempt['status'] != 'OK' and (attempt['detail'] or '').startswith('模型实际运行配置与固定配置不一致（'):
            # Only the exact old producer prefix is a legacy failure signal. Other
            # prose, including negation or a paraphrase, cannot certify an identity.
            checked += 1;failed += 1;legacy += 1
            if len(failure_examples) < 5:
                failure_examples.append({**item, 'code': 'LEGACY_MODEL_IDENTITY_MISMATCH',
                                         'evidence': 'LEGACY_FAILURE_RECORD',
                                         'detail': '历史记录明确拒绝模型身份；缺少可复核的完整身份元数据。'})
        else:
            missing += 1
            if len(missing_examples) < 5:
                missing_examples.append({**item, **verdict, 'status': 'UNKNOWN', 'code': error or time_error or verdict['code'],
                                         'missing': '模型身份元数据或固定配置对应的实际值'})
    status = 'FAIL' if failed else 'INSUFFICIENT' if missing else 'PASS' if checked else 'NOT_APPLICABLE'
    return {'check': 'CHECK_MODEL_IDENTITY', 'status': status, 'checked': checked,
            'failures': failed, 'missing': missing, 'examples': failure_examples + missing_examples,
            'by_route': {'watchlist': {'status': status, 'checked': checked, 'failed': failed,
                                      'missing': missing, 'reused_without_call': reused}},
            'coverage': {'source': 'research_analysis', 'routes': ['watchlist'],
                         'excluded_routes': ['global', 'dynamic', 'industry', 'portfolio', 'review', 'supervision', 'experiment'],
                         'reused_without_call': reused, 'legacy_failures': legacy},
            'detail': '仅核对自选股研究调用中已固定的模型与推理配置；沿用记录不算新调用，缺少身份依据不算通过。'
                      '全球、动态、行业、组合、复盘、监督和实验调用不在本项覆盖范围。'}
