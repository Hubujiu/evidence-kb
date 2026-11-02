from pathlib import Path


WITNESSES = {
    'DeniedReplay': 'mode = "replay" /\\ phase = "error" /\\ scopeAllowed /\\ ~evidenceAllowed /\\ ~evidenceChecked /\\ evidenceAtCheck = authorityRevision',
    'EvidenceRevokedCommit': 'mode = "fresh" /\\ phase = "complete" /\\ scopeAllowed /\\ ~evidenceAllowed /\\ ~commitAllowed /\\ commitAuthority = authorityRevision /\\ commitAuthority # evidenceAtCheck /\\ authorityChecked /\\ evidenceChecked /\\ cancelChecked',
}


def postflight_model_violations(root: Path, registry: dict) -> list[str]:
    module = 'formal/session_authority/PostflightAuthority.tla'
    models = [row for machine in registry['machines'] for row in machine['models'] if row['module'] == module]
    if not (root / module).is_file() and not models:
        return []
    source = ' '.join((root / module).read_text('utf-8').split())
    errors = []
    for assignment in ("commitAllowed' = (scopeAllowed /\\ evidenceAllowed)",
                       "evidenceChecked' = (ValidateReplay /\\ valid)"):
        if source.count(assignment) != 1:
            errors.append('postflight model requires the reviewed complete Boolean RHS: ' + assignment)
    for name, predicate in WITNESSES.items():
        invariant = 'No' + name + 'Witness'
        if f'{invariant} == ~({predicate})' not in source:
            errors.append('postflight model requires the reviewed terminal witness: ' + invariant)
        config = 'formal/session_authority/PostflightAuthority' + name + 'Witness.cfg'
        channel = 'negative_controls' if name == 'DeniedReplay' else 'known_limitations'
        rows = [row for machine in registry['machines'] for row in machine.get(channel, [])
                if row['module'] == module and row['config'] == config and row.get('expected_invariant') == invariant]
        if len(rows) != 1:
            errors.append('postflight model requires registered witness in ' + channel + ': ' + invariant)
        path = root / config
        expected = 'SPECIFICATION Spec CONSTANT ValidateReplay = TRUE INVARIANT ' + invariant + ' CHECK_DEADLOCK FALSE'
        if not path.is_file() or ' '.join(path.read_text('utf-8').split()) != expected:
            errors.append('postflight witness must preserve positive-model behavior: ' + invariant)
    limitations = [row for machine in registry['machines'] for row in machine.get('known_limitations', [])
                   if row['module'] == module and row.get('expected_invariant') == 'CurrentAuthorityAtCommit']
    predicate = ('CurrentAuthorityAtCommit == (mode = "fresh" /\\ phase = "complete") => '
                 '(commitAllowed /\\ commitAuthority = evidenceAtCheck)')
    if len(limitations) != 1 or predicate not in source:
        errors.append('postflight model must retain the independent CurrentAuthorityAtCommit limitation')
    return errors
