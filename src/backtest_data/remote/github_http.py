"""GitHub CLI HTTP framing; callers own budgets, pacing and retries."""
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import subprocess
import sys


MAX_RESPONSE = 4 * 1024**2


@dataclass
class Response:
    status: int
    headers: dict = field(repr=False)
    data: object = field(repr=False)


def _response(wire, *, raw):
    if len(wire) > MAX_RESPONSE:
        raise ValueError('bounded GitHub publication response required')
    status = 0
    headers = {}
    content = wire
    while content.startswith(b'HTTP/'):
        boundaries = [(index, separator) for index, separator in (
            (content.find(b'\r\n\r\n'), 4), (content.find(b'\n\n'), 2)) if index >= 0]
        if not boundaries:
            raise ValueError('GitHub response header proof missing')
        end, separator = min(boundaries)
        block = content[:end].replace(b'\r\n', b'\n')
        content = content[end + separator:]
        lines = block.split(b'\n')
        match = re.fullmatch(rb'HTTP/\S+ ([1-5]\d{2})(?: (.*))?', lines[0])
        if not match:
            raise ValueError('GitHub response status proof missing')
        status = int(match[1])
        headers = {}
        for line in lines[1:]:
            name, delimiter, value = line.partition(b':')
            if not delimiter or not name:
                raise ValueError('GitHub response header proof missing')
            try:
                headers[name.decode('ascii').lower()] = value.decode('latin1').strip()
            except UnicodeError:
                raise ValueError('GitHub response header proof missing') from None
        # Only interim, redirect and proxy CONNECT headers can precede another
        # response. A successful binary asset can itself start with HTTP/.
        if not (100 <= status < 200 or 300 <= status < 400 or
                match[2] == b'Connection established'):
            break
    if not status:
        return Response(0, {}, None)
    if raw and 200 <= status < 300:
        return Response(status, headers, content)
    try:
        data = json.loads(content) if content.strip() else None
    except (ValueError, UnicodeError):
        if 200 <= status < 300:
            raise ValueError('GitHub response JSON proof missing') from None
        data = None
    return Response(status, headers, data)


class GhAPI:
    """Use existing gh authentication without exposing private CLI output."""

    def __init__(self, gh):
        self.gh = str(gh)

    def request(self, method, url, *, body=None, input_file=None, raw=False, timeout=300):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('positive finite GitHub HTTP timeout required')
        command = [self.gh, 'api', url, '--method', method, '--include',
                   '-H', 'Accept: ' + ('application/octet-stream' if raw else 'application/vnd.github+json'),
                   '-H', 'X-GitHub-Api-Version: 2022-11-28']
        payload = None
        if input_file is not None:
            command.extend(['-H', 'Content-Type: application/octet-stream',
                            '-H', 'Content-Length: ' + str(Path(input_file).stat().st_size),
                            '--input', str(input_file)])
        elif body is not None:
            command.extend(['-H', 'Content-Type: application/json', '--input', '-'])
            payload = json.dumps(body).encode()
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        try:
            result = subprocess.run(command, input=payload, capture_output=True,
                                    timeout=min(300, timeout), **options)
        except (subprocess.TimeoutExpired, OSError):
            return Response(0, {}, None)
        # stderr and CLI exit text do not prove an HTTP response status.
        return _response(result.stdout, raw=raw)
