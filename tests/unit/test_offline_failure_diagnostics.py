import json
from scripts import offline_publication as publication


def test_diagnostics_keep_location_and_known_contract_without_private_exception_text():
    try:
        raise ValueError('duplicate capture in publication')
    except ValueError as error:
        details = publication.error_details(error)
    assert details['reason'] == 'duplicate_capture_in_publication'
    assert details['locations'][-1]['function'].startswith('test_diagnostics_')
    try:
        raise ValueError('private response token=do-not-log-this')
    except ValueError as error:
        private = publication.error_details(error)
    assert 'do-not-log-this' not in json.dumps(private)
    assert 'reason' not in private


def test_diagnostics_preserve_typed_github_status_without_printing_request():
    details = publication.error_details(publication.GitHubError(503, 'release_read'))
    assert details['http_status'] == 503
    assert details['operation'] == 'release_read'
