"""Knowledge-owned state initialization and failure-response policy."""


def initial_state(source_policy, config_name='recommended'):
    return {'version': 'agentic-rag/5.0', 'source_policy': source_policy,
            'config_name': config_name, 'turn_number': 0, 'topic': '', 'constraints': [],
            'evidence': {}, 'checkpoints': {}, 'last_answer': None, 'corrections': [],
            'source_epoch': 0, 'authority': None}


def validate_failure(response):
    if response.get('status') != 'error' or response.get('answer') or response.get('evidence') or response.get('claims'):
        raise ValueError('INVALID_FAILURE_RESPONSE')
