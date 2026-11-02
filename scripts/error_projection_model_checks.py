from pathlib import Path


WITNESSES = {
    'Coded': 'kind = "coded" /\\ phase = "observed" /\\ publicCode = "domain-code" /\\ event = source',
    'GenericAbsent': 'kind = "generic" /\\ phase = "observed" /\\ source = Missing /\\ event = source',
    'GenericUnsent': 'kind = "generic" /\\ phase = "observed" /\\ source.attempts = "zero" /\\ source.sent = "no" /\\ event = source',
    'GenericUnknown': 'kind = "generic" /\\ phase = "observed" /\\ source.attempts = "one" /\\ source.usage = "unknown" /\\ source.sent = "unknown" /\\ event = source',
    'GenericZero': 'kind = "generic" /\\ phase = "observed" /\\ source.usage = "zero" /\\ source.sent = "yes" /\\ event = source',
    'GenericPositive': 'kind = "generic" /\\ phase = "observed" /\\ source.usage = "one" /\\ source.sent = "yes" /\\ event = source',
    'CancelUnknown': 'kind = "cancel" /\\ phase = "cancelled" /\\ source.attempts = "one" /\\ source.usage = "unknown" /\\ originalRaised /\\ ~pending /\\ ledger = 1',
}


def error_projection_model_violations(root: Path, registry: dict) -> list[str]:
    module = 'formal/cache_evidence/ErrorProjection.tla'
    models = [row for machine in registry['machines'] for row in machine['models'] if row['module'] == module]
    if not (root / module).is_file() and not models:
        return []
    errors = []
    source = (root / module).read_text('utf-8')
    assignment = '/\\ privateVisible\' = (LeakGenericText /\\ kind = "generic")'
    if source.count(assignment) != 1:
        errors.append('error projection model requires the reviewed parenthesized Boolean RHS')
    controls = [row for machine in registry['machines'] for row in machine['negative_controls']]
    for name, predicate in WITNESSES.items():
        invariant = 'No' + name + 'Witness'
        if f'{invariant} == ~({predicate})' not in source:
            errors.append('error projection model requires terminal reachability predicate: ' + invariant)
        config = 'formal/cache_evidence/ErrorProjection' + name + 'Witness.cfg'
        registered = [row for row in controls if row['module'] == module and row['config'] == config
                      and row.get('expected_invariant') == invariant]
        if len(registered) != 1:
            errors.append('error projection model requires registered terminal witness: ' + invariant)
        path = root / config
        expected = ('SPECIFICATION Spec CONSTANTS DropGenericFacts = FALSE LeakGenericText = FALSE '
                    'InventUnsent = FALSE INVARIANT ' + invariant + ' CHECK_DEADLOCK FALSE')
        if not path.is_file() or ' '.join(path.read_text('utf-8').split()) != expected:
            errors.append('error projection witness must explore the unmodified positive behavior: ' + invariant)
    return errors
