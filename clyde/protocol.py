import json
import re
import socket
import time

MAX_MESSAGE = 16384
MAX_DOCUMENT = 2048
TOKEN = re.compile(r'[a-zA-Z0-9_-]{1,64}\Z')

class ProtocolError(Exception):
    pass

def token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None

def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError('duplicate field')
            result[key] = value
        return result
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('constant')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError('invalid JSON') from exc
    if not isinstance(result, dict):
        raise ProtocolError('object required')
    return result

def receive(sock, deadline):
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('deadline exceeded')
        sock.settimeout(remaining)
        byte = sock.recv(1)
        if not byte:
            raise ProtocolError('truncated frame')
        if byte == b'\n':
            return decode(bytes(data))
        data.extend(byte)
        if len(data) > MAX_MESSAGE:
            raise ProtocolError('message too large')

def send(sock, message, deadline):
    data = json.dumps(message, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ProtocolError('message too large')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('deadline exceeded')
    sock.settimeout(remaining)
    sock.sendall(data + b'\n')
