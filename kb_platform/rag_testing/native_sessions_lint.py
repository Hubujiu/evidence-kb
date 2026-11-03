"""Native table owner guard independent of source digest refresh."""
import re


def native_sessions_violations(source, model):
    text = re.sub(r'\s+', '', source)
    required = {
        'serialized owner': '.get_or_init(Default::default).lock().map_err(|_|state_error())?',
        'expiry before admission': '.retain(|_,session|session.touched.elapsed()<Duration::from_secs(300));',
        'bounded capacity': 'ifsessions.values.len()>=64',
        'non-reused identity': 'sessions.next=sessions.next.checked_add(1).ok_or_else(state_error)?;',
        'finish consumes handle': 'letmutsession=sessions.values.remove(&handle).ok_or_else(state_error)?;',
        'input admission': 'ifsession.phase!=Phase::Input',
        'result admission': 'ifsession.phase!=Phase::Executing',
        'failure invalidates codec': 'session.codec=None;session.phase=Phase::Failed;',
        'values-only terminal': 'ifsession.phase==Phase::Values{returnOk(json!({"count":values?.len()}));}',
    }
    errors = [label for label, pattern in required.items() if pattern not in text]
    compact = re.sub(r'\s+', '', model)
    if (r"expired'=expired\cupstale" not in compact
            or 'WitnessExpiry==~(expired#{}' not in compact
            or r"failed'=failed\cup{h}" not in compact
            or r'WitnessFailedCleanup==~(failed\intersectremoved#{}' not in compact):
        errors.append('expiry and failed cleanup witnesses require causal transition history')
    return errors
