"""Inherited by CI/test subprocesses; permit only the DB and local controlled HTTP."""
import ipaddress
import os
import socket

if os.getenv('M13_BLOCK_EXTERNAL') == '1':
    allowed = {'127.0.0.1', '::1', 'localhost', os.getenv('DB_HOST', 'db')}
    try:
        allowed.update(r[4][0] for r in socket.getaddrinfo(os.getenv('DB_HOST', 'db'), None))
    except OSError:
        pass
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def check(address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if host not in allowed and not local:
                raise PermissionError('M13 controlled checks prohibit external network connections')
    def connect(self, address):
        check(address)
        return original_connect(self, address)
    def connect_ex(self, address):
        check(address)
        return original_connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
