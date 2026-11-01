"""Count live tool-step outcomes from private TOOLCALL_LOG traces, offline.

Usage: python -m server.trace_report logs
Reads only local trace files and makes no account or network requests. Output
contains counts and fixed bridge error categories, never prompts, replies,
tool arguments or conversation IDs.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

from .toolcall_logging import SEPARATOR

_NAMES = {line: name for name, line in SEPARATOR.items()}
_DATA = re.compile(r'(?m)^[ \t]*data:[ \t]*')

# Bridge error templates, by who produced the failure. A model step is one the
# provider generated; bridge-state and request refusals say nothing about it.
_CATEGORIES = (
    ('model_format', (
        'Invalid or incomplete DSML', 'Unsupported tool-call markup',
        'The tool_calls block contains invalid JSON', 'Expected one complete fenced',
        'Each tool call must be an object', 'Tool arguments contain invalid JSON',
        'Tool arguments must be a JSON object', 'Tool arguments contain non-finite',
        'Tool call contains ambiguous envelope', 'Provider returned no answer text')),
    ('model_schema', (
        'Tool arguments do not satisfy', 'The model called an unadvertised tool',
        'The model called a tool', 'The model did not emit the required',
        'The model started a tool call while')),
    ('model_incomplete', (
        'The provider reached its output limit', 'The provider did not finish',
        'Could not finish the truncated', 'The format correction did not finish')),
)
_REPAIR_FAILED = 'The single format correction also failed; no further correction was requested. '
MODEL_OUTCOMES = ('calls', 'text', 'model_format', 'model_schema', 'model_incomplete')


def _blocks(text):
    name, body = None, []
    for line in text.splitlines():
        if line in _NAMES:
            if name:
                yield name, '\n'.join(body).strip()
            name, body = _NAMES[line], []
        elif name:
            body.append(line)
    if name:
        yield name, '\n'.join(body).strip()


def _responses(text):
    """Join consecutive to_agent blocks: each SSE frame is recorded separately."""
    frames = []
    for name, body in _blocks(text):
        if name == 'to_agent':
            frames.append(body)
        elif frames:
            yield '\n'.join(frames)
            frames = []
    if frames:
        yield '\n'.join(frames)


def _objects(body):
    if body.startswith('{'):
        try:
            return [json.loads(body)]
        except ValueError:
            return []
    decoder, objects = json.JSONDecoder(), []
    for match in _DATA.finditer(body):
        try:
            objects.append(decoder.raw_decode(body, match.end())[0])
        except ValueError:
            continue
    return objects


def classify(body):
    finish, error = None, None
    for item in _objects(body):
        if not isinstance(item, dict):
            continue
        if isinstance(item.get('error'), dict):
            error = item['error']
        for choice in item.get('choices') or ():
            finish = choice.get('finish_reason') or finish
    if error is None:
        return {'tool_calls': 'calls', 'stop': 'text'}.get(finish, 'unknown')
    if error.get('type') != 'invalid_tool_response':
        return 'request_refused'
    return error_category(str(error.get('message') or ''))


def error_category(message):
    """Who produced an invalid_tool_response: model_* or bridge_state."""
    if message.startswith(_REPAIR_FAILED):
        # The bridge prefixes the corrected reply's own error: classify that.
        message = message[len(_REPAIR_FAILED):]
        for category, prefixes in _CATEGORIES:
            if message.startswith(prefixes):
                return category
        return 'model_format'
    for category, prefixes in _CATEGORIES:
        if message.startswith(prefixes):
            return category
    return 'bridge_state'


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


_LOG_PROVIDER = re.compile(r'^\d{10}-(.+?)(?:-\d+)?\.log$')


class _Settle:
    """Per-provider settle tallies: turn counts, value samples and path groups."""

    def __init__(self):
        self.counts = Counter()
        self.samples = {}
        self.groups = {}

    def sample(self, name, value, high=10_000_000):
        if _count(value) and value <= high:
            self.samples.setdefault(name, []).append(value)

    def group(self, name, key):
        self.groups.setdefault(name, Counter())[key] += 1

    def summary(self):
        out = dict(sorted(self.counts.items()))
        for name, values in sorted(self.samples.items()):
            out[name] = {'count': len(values), 'min': min(values), 'max': max(values),
                         'avg': round(sum(values) / len(values))}
        for name, counter in sorted(self.groups.items()):
            out[name] = dict(counter.most_common(10))
        return out


def _common_settle(data, tally):
    """Fields every reporting provider shares (docs/BRIDGE_TRACE_STATS.ru.md)."""
    counts = tally.counts
    counts['turns'] += 1
    if data.get('source') == 'dom':
        counts['source_dom'] += 1
    if data.get('tool') is True:
        counts['tool_envelope'] += 1
    late = data.get('late_rewrite')
    if isinstance(late, dict):
        counts['late_rewrite'] += 1
        tally.sample('late_rewrite_delay_ms', late.get('delay_ms'))
    dom = data.get('dom_check')
    if not isinstance(dom, dict):
        return
    counts['dom_check'] += 1
    if dom.get('match') is False:
        counts['dom_mismatch'] += 1
        # Every emitted word is on the page, which only adds text: a rendered
        # widget, not a different answer. Anything else needs a look.
        covered = _count(dom.get('coverage')) and dom['coverage'] >= 97 \
            and _count(dom.get('wire_len')) and _count(dom.get('dom_len')) \
            and dom['dom_len'] > dom['wire_len']
        counts['dom_render_extra' if covered else 'dom_suspect'] += 1
        # The agent acted on a tool call the page no longer shows, or missed one it shows.
        if isinstance(dom.get('tool_wire'), bool) and isinstance(dom.get('tool_dom'), bool) \
                and dom['tool_wire'] != dom['tool_dom']:
            counts['dom_tool_mismatch'] += 1
    if _count(dom.get('changes')) and dom['changes'] > 0:
        counts['dom_changed_after_emit'] += 1
    tally.sample('dom_similarity', dom.get('similarity'), 100)
    tally.sample('dom_coverage', dom.get('coverage'), 100)


# Provider (log file suffix) -> counter of its own settle fields.
_SETTLE_EXTRA = {}


def _settle_stats(text, provider, tally):
    """Count settle metadata; numbers only, never prompt/reply text."""
    extra = _SETTLE_EXTRA.get(provider)
    for name, body in _blocks(text):
        if name != 'bridge_settle':
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        _common_settle(data, tally)
        if extra is not None:
            extra(data, tally)


def report(log_dir, since=None):
    """Summarize traces; with ``since`` (epoch seconds) only newer files."""
    days, total = {}, Counter()
    settle = {}
    for path in sorted(Path(log_dir).glob('*.log')):
        if since is not None and path.stat().st_mtime < since:
            continue
        day = path.name[:4] if path.name[:4].isdigit() else 'unknown'
        text = path.read_text(encoding='utf-8', errors='replace')
        for body in _responses(text):
            outcome = classify(body)
            total[outcome] += 1
            days.setdefault(day, Counter())[outcome] += 1
        match = _LOG_PROVIDER.match(path.name)
        provider = match.group(1) if match else 'unknown'
        tally = settle.setdefault(provider, _Settle())
        _settle_stats(text, provider, tally)

    def summary(counts):
        model = sum(counts[key] for key in MODEL_OUTCOMES)
        ok = counts['calls'] + counts['text']
        return {'outcomes': dict(sorted(counts.items())), 'model_steps': model,
                'model_step_success': round(ok / model, 3) if model else None}

    result = {**summary(total), 'by_day': {day: summary(counts) for day, counts in sorted(days.items())}}
    by_provider = {name: tally.summary() for name, tally in sorted(settle.items()) if tally.counts}
    if by_provider:
        result['bridge_settle'] = by_provider
    return result


if __name__ == '__main__':
    print(json.dumps(report(sys.argv[1] if len(sys.argv) > 1 else 'logs'), indent=2))
